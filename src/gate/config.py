"""Cargador y validación de configuración declarativa para Lucid Trading.

Todas las horas de las reglas se expresan en la hora oficial del CME (America/Chicago, CT).
"""
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Dict, Optional, Literal
import yaml

logger = logging.getLogger("gate.config")


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    timeout_ms: int = 200
    heartbeat_interval_seconds: int = 5
    default_fail_action: Literal["hold", "flatten"] = "flatten"
    audit_log_path: str = "logs/gate_audit.log"
    log_timezone: str = "America/Chicago"  # Zona horaria de logs y timestamps (reemplaza UTC)


@dataclass(frozen=True)
class SessionConfig:
    mode: Literal["24h_with_break", "rth_only"] = "24h_with_break"
    timezone: str = "America/Chicago"   # Hora oficial CME (CST invierno / CDT verano)
    daily_break_start: str = "16:00"    # 16:00 CT = 17:00 ET: inicio del break de mantenimiento
    daily_break_end: str = "17:00"      # 17:00 CT = 18:00 ET: reapertura Globex
    pre_break_buffer_minutes: int = 5   # Bloqueo de entradas 5 min antes (15:55 CT)


@dataclass(frozen=True)
class InstrumentConfig:
    type: Literal["mini", "micro"]
    point_value: float
    tick_size: float
    tick_value: float
    description: str = ""


@dataclass(frozen=True)
class AccountPreset:
    tier: str
    phase: Literal["eval", "funded", "payout"]
    initial_balance: float
    profit_target: Optional[float]
    daily_loss_limit: float
    max_loss_limit: float
    drawdown_type: Literal["EOD", "intraday"]
    consistency_cap_pct: Optional[float]
    # Eval: balance inicial del piso de liquidacion.
    # Funded: balance minimo bajo el cual se pierde la cuenta (tope del trailing drawdown).
    min_account_balance: float
    max_contracts_mini: int
    max_contracts_micro: int
    min_trade_duration_seconds: float = 10.0
    session_start_time: str = "08:30"    # Hora de Chicago (solo modo rth_only)
    session_flatten_time: str = "14:55"  # Hora de Chicago (solo modo rth_only)
    lock_day_after_consistency_cap: bool = True
    min_days_of_profit: Optional[str] = None
    min_profit_day_amount: Optional[float] = None
    min_profit_days_required: int = 5
    lock_day_after_qualifying_profit: bool = True
    days_to_payout: Optional[int] = None
    scaling_plan: bool = False
    # Solo Funded: balance_for_payout = initial_balance + buffer + payout
    buffer: Optional[float] = None
    payout: Optional[float] = None

    @property
    def is_funded_like(self) -> bool:
        """Funded y la fase posterior al primer retiro (payout) comparten piso, bloqueo de dia y elegibilidad."""
        return self.phase in ("funded", "payout")

    @property
    def balance_for_payout(self) -> Optional[float]:
        if self.buffer is None or self.payout is None:
            return None
        return self.initial_balance + self.buffer + self.payout


@dataclass
class GateConfig:
    server: ServerConfig
    session: SessionConfig = field(default_factory=SessionConfig)
    instruments: Dict[str, InstrumentConfig] = field(default_factory=dict)
    presets: Dict[str, AccountPreset] = field(default_factory=dict)
    accounts: Dict[str, str] = field(default_factory=dict)  # account_name -> preset_id

    def get_preset_for_account(self, account_name: str) -> Optional[AccountPreset]:
        preset_id = self.accounts.get(account_name)
        if not preset_id:
            return None
        return self.presets.get(preset_id)


def _parse_min_profit(val: Optional[str]) -> Optional[float]:
    if not val:
        return None
    import re
    m = re.search(r"\$(\d+)", val)
    return float(m.group(1)) if m else None


def load_config(file_path: str | Path = "config/lucid_rules.yaml") -> GateConfig:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path.resolve()}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Formato invalido en {path}: se esperaba un mapeo yaml")

    srv_data = data.get("server", {})
    server = ServerConfig(
        host=srv_data.get("host", "127.0.0.1"),
        port=int(srv_data.get("port", 8765)),
        timeout_ms=int(srv_data.get("timeout_ms", 200)),
        heartbeat_interval_seconds=int(srv_data.get("heartbeat_interval_seconds", 5)),
        default_fail_action=srv_data.get("default_fail_action", "flatten"),
        audit_log_path=srv_data.get("audit_log_path", "logs/gate_audit.log"),
        log_timezone=srv_data.get("log_timezone", "America/Chicago"),
    )

    sess_data = data.get("session", {})
    session = SessionConfig(
        mode=sess_data.get("mode", "24h_with_break"),
        timezone=sess_data.get("timezone", "America/Chicago"),
        daily_break_start=sess_data.get("daily_break_start", "16:00"),
        daily_break_end=sess_data.get("daily_break_end", "17:00"),
        pre_break_buffer_minutes=int(sess_data.get("pre_break_buffer_minutes", 5)),
    )

    instruments: Dict[str, InstrumentConfig] = {}
    for inst_name, inst_data in data.get("instruments", {}).items():
        instruments[inst_name] = InstrumentConfig(
            type=inst_data["type"],
            point_value=float(inst_data["point_value"]),
            tick_size=float(inst_data["tick_size"]),
            tick_value=float(inst_data["tick_value"]),
            description=inst_data.get("description", ""),
        )

    presets: Dict[str, AccountPreset] = {}
    for pid, pdata in data.get("presets", {}).items():
        presets[pid] = AccountPreset(
            tier=pdata["tier"],
            phase=pdata["phase"],
            initial_balance=float(pdata["initial_balance"]),
            profit_target=float(pdata["profit_target"]) if pdata.get("profit_target") is not None else None,
            daily_loss_limit=float(pdata["daily_loss_limit"]),
            max_loss_limit=float(pdata["max_loss_limit"]),
            drawdown_type=pdata.get("drawdown_type", "EOD"),
            consistency_cap_pct=float(pdata["consistency_cap_pct"]) if pdata.get("consistency_cap_pct") is not None else None,
            min_account_balance=float(pdata["min_account_balance"]),
            max_contracts_mini=int(pdata["max_contracts_mini"]),
            max_contracts_micro=int(pdata["max_contracts_micro"]),
            min_trade_duration_seconds=float(pdata.get("min_trade_duration_seconds", 10.0)),
            session_start_time=pdata.get("session_start_time", "08:30"),
            session_flatten_time=pdata.get("session_flatten_time", "14:55"),
            lock_day_after_consistency_cap=bool(pdata.get("lock_day_after_consistency_cap", True)),
            min_days_of_profit=pdata.get("min_days_of_profit"),
            min_profit_day_amount=float(pdata["min_profit_day_amount"]) if pdata.get("min_profit_day_amount") is not None else _parse_min_profit(pdata.get("min_days_of_profit")),
            min_profit_days_required=int(pdata.get("min_profit_days_required", 5)),
            lock_day_after_qualifying_profit=bool(pdata.get("lock_day_after_qualifying_profit", True)),
            days_to_payout=int(pdata["days_to_payout"]) if pdata.get("days_to_payout") is not None else None,
            scaling_plan=bool(pdata.get("scaling_plan", False)),
            buffer=float(pdata["buffer"]) if pdata.get("buffer") is not None else None,
            payout=float(pdata["payout"]) if pdata.get("payout") is not None else None,
        )
        if presets[pid].is_funded_like and presets[pid].balance_for_payout is None:
            logger.warning(
                "Preset '%s' (funded/payout) sin 'buffer'/'payout': balance_for_payout no definido, "
                "el gate no aplicara la logica de payout hasta configurarlos.", pid
            )

    accounts = {str(k): str(v) for k, v in data.get("accounts", {}).items()}

    # Validar integridad referencial
    for acc, pid in accounts.items():
        if pid not in presets:
            raise ValueError(f"Cuenta '{acc}' referencia preset desconocido '{pid}'")

    return GateConfig(server=server, session=session, instruments=instruments, presets=presets, accounts=accounts)
