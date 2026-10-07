"""Motor de riesgo en Python para cuentas de fondeo Lucid Trading.
Evalua cada solicitud de orden contra las reglas de la cuenta (EOD Trailing DD, DLL,
limite de contratos, consistencia, horarios y stop obligatorio).
"""
from dataclasses import dataclass, field
from datetime import datetime, time
import logging
from typing import Dict, List, Optional
import zoneinfo

from src.gate.config import GateConfig, AccountPreset

logger = logging.getLogger("gate.risk_engine")


@dataclass
class PositionState:
    account: str
    instrument: str
    qty: int
    entry_price: float = 0.0
    side: str = "FLAT"  # "LONG", "SHORT", "FLAT"
    open_time: Optional[datetime] = None


@dataclass
class TelemetryUpdate:
    account: str
    strategy_id: str
    current_balance: Optional[float] = None
    realized_pnl_today: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    positions: Optional[List[PositionState]] = None


@dataclass
class AuthRequest:
    request_id: str
    strategy_id: str
    account: str
    instrument: str
    side: str  # "BUY" | "SELL"
    qty: int
    stop_distance: float  # En puntos/ticks (> 0)


@dataclass
class AuthResponse:
    request_id: str
    allow: bool
    max_qty: int
    reason: str


@dataclass
class AccountState:
    account_name: str
    preset: AccountPreset
    current_balance: float
    day_start_balance: float
    eod_hwm: float
    realized_pnl_today: float = 0.0
    unrealized_pnl: float = 0.0
    current_positions: Dict[str, PositionState] = field(default_factory=dict)
    is_flattened: bool = False
    is_paused: bool = False


class RiskEngine:
    def __init__(self, config: GateConfig):
        self.config = config
        self.accounts: Dict[str, AccountState] = {}
        self.strategy_paused: Dict[str, bool] = {}
        self.ny_tz = zoneinfo.ZoneInfo("America/New_York")

    def register_account(self, account_name: str, balance: Optional[float] = None) -> AccountState:
        preset = self.config.get_preset_for_account(account_name)
        if not preset:
            raise ValueError(f"No hay preset configurado para la cuenta '{account_name}'")

        init_bal = balance if balance is not None else preset.initial_balance
        state = AccountState(
            account_name=account_name,
            preset=preset,
            current_balance=init_bal,
            day_start_balance=init_bal,
            eod_hwm=init_bal,
        )
        self.accounts[account_name] = state
        return state

    def get_account_state(self, account_name: str) -> Optional[AccountState]:
        return self.accounts.get(account_name)

    def pause_strategy(self, strategy_id: str, paused: bool = True) -> None:
        self.strategy_paused[strategy_id] = paused
        logger.info("Estrategia %s %s", strategy_id, "PAUSADA" if paused else "REANUDADA")

    def flatten_account(self, account_name: str) -> None:
        acc = self.accounts.get(account_name)
        if acc:
            acc.is_flattened = True
            logger.warning("Cuenta %s marcada para FLATTEN obligatorio", account_name)

    def reconcile_account(self, account_name: str, positions: List[PositionState]) -> None:
        acc = self.accounts.get(account_name)
        if not acc:
            acc = self.register_account(account_name)

        acc.current_positions.clear()
        for p in positions:
            acc.current_positions[p.instrument] = p
        logger.info("Cuenta %s reconciliada con %d posiciones activas", account_name, len(positions))

    def update_telemetry(self, t: TelemetryUpdate) -> None:
        acc = self.accounts.get(t.account)
        if not acc:
            acc = self.register_account(t.account)

        if t.current_balance is not None:
            acc.current_balance = t.current_balance
            if acc.current_balance > acc.eod_hwm:
                acc.eod_hwm = acc.current_balance

        if t.realized_pnl_today is not None:
            acc.realized_pnl_today = t.realized_pnl_today

        if t.unrealized_pnl is not None:
            acc.unrealized_pnl = t.unrealized_pnl

        if t.positions is not None:
            acc.current_positions.clear()
            for p in t.positions:
                acc.current_positions[p.instrument] = p

    def evaluate_authorization(
        self, req: AuthRequest, current_time: Optional[datetime] = None
    ) -> AuthResponse:
        # 1. Validación de Stop Loss obligatorio (Criterio 8)
        if req.stop_distance <= 0:
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason="STOP_REQUIRED: Toda entrada debe tener un stop loss predefinido > 0",
            )

        # 2. Verificación de existencia de cuenta
        acc = self.accounts.get(req.account)
        if not acc:
            try:
                acc = self.register_account(req.account)
            except ValueError as e:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"ACCOUNT_UNKNOWN: {str(e)}",
                )

        preset = acc.preset

        # 3. Verificación de pausa de estrategia o cuenta (Criterio 7)
        if self.strategy_paused.get(req.strategy_id, False):
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason=f"STRATEGY_PAUSED: La estrategia '{req.strategy_id}' se encuentra en pausa",
            )

        if acc.is_paused or acc.is_flattened:
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason=f"ACCOUNT_PAUSED_OR_FLATTENED: La cuenta '{req.account}' esta pausada o en flatten",
            )

        # 4. Horarios de sesión y aplanado obligatorio (15:55 ET) (Criterio 9)
        now_et = current_time if current_time is not None else datetime.now(self.ny_tz)
        t_now = now_et.time()
        start_t = time.fromisoformat(preset.session_start_time_et)
        flatten_t = time.fromisoformat(preset.session_flatten_time_et)

        if not (start_t <= t_now < flatten_t):
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason=f"OUTSIDE_TRADING_HOURS: Hora actual {t_now.strftime('%H:%M')} fuera del rango permitido ({preset.session_start_time_et} - {preset.session_flatten_time_et} ET)",
            )

        # 5. Drawdown EOD y piso de liquidación (Criterio 9)
        current_equity = acc.current_balance + acc.unrealized_pnl
        # Piso EOD: min_account_balance inicial o el trailing EOD
        trailing_floor = acc.eod_hwm - preset.max_loss_limit
        effective_floor = max(preset.min_account_balance, trailing_floor)

        if current_equity <= effective_floor:
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason=f"DRAWDOWN_LIMIT_BREACHED: Equity ({current_equity:.2f}) ha alcanzado el piso de liquidacion ({effective_floor:.2f})",
            )

        # 6. Daily Loss Limit (DLL) (Criterio 9)
        # Perdida acumulada hoy = -(PnL realizado hoy + perdida no realizada actual)
        net_daily_pnl = acc.realized_pnl_today + min(0.0, acc.unrealized_pnl)
        if net_daily_pnl <= -preset.daily_loss_limit:
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason=f"DAILY_LOSS_LIMIT_REACHED: Perdida del dia ({abs(net_daily_pnl):.2f}) alcanzo el DLL ({preset.daily_loss_limit:.2f})",
            )

        # 7. Regla de consistencia del 50% de Lucid (Criterio 9)
        if preset.phase == "eval" and preset.consistency_cap_pct and preset.profit_target:
            max_single_day_profit = preset.profit_target * preset.consistency_cap_pct
            if acc.realized_pnl_today >= max_single_day_profit:
                logger.warning(
                    "Cuenta %s: Profit de hoy ($%.2f) alcanzo el tope de consistencia de 50%% ($%.2f). Operar mas puede comprometer la regla.",
                    req.account,
                    acc.realized_pnl_today,
                    max_single_day_profit,
                )

        # 8. Limite de contratos NQ / MNQ (Criterio 9)
        inst_info = self.config.instruments.get(req.instrument)
        is_micro = (inst_info and inst_info.type == "micro") or req.instrument == "MNQ"
        max_allowed = preset.max_contracts_micro if is_micro else preset.max_contracts_mini

        current_qty = 0
        if req.instrument in acc.current_positions:
            current_qty = acc.current_positions[req.instrument].qty

        available_qty = max_allowed - current_qty
        if req.qty > available_qty:
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=max(0, available_qty),
                reason=f"MAX_CONTRACTS_EXCEEDED: Solicitados {req.qty}, cupo disponible {available_qty} (maximo total {max_allowed})",
            )

        # Todo OK -> Autorizado
        return AuthResponse(
            request_id=req.request_id,
            allow=True,
            max_qty=req.qty,
            reason="APPROVED",
        )
