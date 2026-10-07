"""Cargador y validación de configuración declarativa para Lucid Trading."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Literal
import yaml


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    timeout_ms: int = 200
    heartbeat_interval_seconds: int = 5
    default_fail_action: Literal["hold", "flatten"] = "flatten"
    audit_log_path: str = "logs/gate_audit.log"


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
    phase: Literal["eval", "funded"]
    initial_balance: float
    profit_target: Optional[float]
    daily_loss_limit: float
    max_loss_limit: float
    drawdown_type: Literal["EOD", "intraday"]
    consistency_cap_pct: Optional[float]
    min_account_balance: float
    max_contracts_mini: int
    max_contracts_micro: int
    min_trade_duration_seconds: float = 10.0
    session_start_time_et: str = "09:30"
    session_flatten_time_et: str = "15:55"
    min_days_of_profit: Optional[str] = None
    days_to_payout: Optional[int] = None
    scaling_plan: bool = False


@dataclass
class GateConfig:
    server: ServerConfig
    instruments: Dict[str, InstrumentConfig] = field(default_factory=dict)
    presets: Dict[str, AccountPreset] = field(default_factory=dict)
    accounts: Dict[str, str] = field(default_factory=dict)  # account_name -> preset_id

    def get_preset_for_account(self, account_name: str) -> Optional[AccountPreset]:
        preset_id = self.accounts.get(account_name)
        if not preset_id:
            return None
        return self.presets.get(preset_id)


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
            session_start_time_et=pdata.get("session_start_time_et", "09:30"),
            session_flatten_time_et=pdata.get("session_flatten_time_et", "15:55"),
            min_days_of_profit=pdata.get("min_days_of_profit"),
            days_to_payout=int(pdata["days_to_payout"]) if pdata.get("days_to_payout") is not None else None,
            scaling_plan=bool(pdata.get("scaling_plan", False)),
        )

    accounts = {str(k): str(v) for k, v in data.get("accounts", {}).items()}

    # Validar integridad referencial
    for acc, pid in accounts.items():
        if pid not in presets:
            raise ValueError(f"Cuenta '{acc}' referencia preset desconocido '{pid}'")

    return GateConfig(server=server, instruments=instruments, presets=presets, accounts=accounts)
