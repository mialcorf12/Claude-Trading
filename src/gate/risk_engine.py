"""Motor de riesgo en Python para cuentas de fondeo Lucid Trading.
Evalua cada solicitud de orden contra las reglas de la cuenta (EOD Trailing DD, DLL,
limite de contratos, consistencia, horarios y stop obligatorio).
"""
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
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
    qualifying_days_history: List[float] = field(default_factory=list)


class RiskEngine:
    def __init__(self, config: GateConfig):
        self.config = config
        self.accounts: Dict[str, AccountState] = {}
        self.strategy_paused: Dict[str, bool] = {}
        self.ny_tz = zoneinfo.ZoneInfo("America/New_York")
        self.cme_tz = zoneinfo.ZoneInfo(config.session.timezone)

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

    def record_closed_day(self, account_name: str, day_profit: float) -> bool:
        """Registra el profit final de una jornada. Si cumple con el minimo de dia ganador en Funded, lo suma al historial."""
        acc = self.accounts.get(account_name)
        if not acc:
            return False
        
        min_amount = acc.preset.min_profit_day_amount or 0.0
        if acc.preset.phase == "funded" and day_profit >= min_amount and min_amount > 0:
            acc.qualifying_days_history.append(day_profit)
            logger.info(
                "Cuenta %s: Dia calificado registrado ($%.2f >= $%.2f). Total calificados: %d/%d",
                account_name, day_profit, min_amount, len(acc.qualifying_days_history), acc.preset.min_profit_days_required
            )
            return True
        return False

    def get_payout_status(self, account_name: str) -> Dict[str, object]:
        """Calcula el estado de cumplimiento de los requisitos de payout para cuentas funded."""
        acc = self.accounts.get(account_name)
        if not acc:
            return {"error": f"Cuenta {account_name} no registrada"}
        
        preset = acc.preset
        is_today_qualifying = (
            preset.min_profit_day_amount is not None
            and acc.realized_pnl_today >= preset.min_profit_day_amount
        )
        total_qualifying = len(acc.qualifying_days_history) + (1 if is_today_qualifying else 0)
        has_min_days = total_qualifying >= preset.min_profit_days_required
        has_min_balance = acc.current_balance >= preset.min_account_balance

        return {
            "account": account_name,
            "phase": preset.phase,
            "current_balance": acc.current_balance,
            "min_account_balance": preset.min_account_balance,
            "realized_pnl_today": acc.realized_pnl_today,
            "min_profit_day_amount": preset.min_profit_day_amount,
            "qualified_days_count": total_qualifying,
            "required_qualifying_days": preset.min_profit_days_required,
            "is_today_qualifying": is_today_qualifying,
            "has_min_days": has_min_days,
            "has_min_balance": has_min_balance,
            "is_payout_eligible": has_min_days and has_min_balance,
        }

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
            if p.qty > 0 and p.side != "FLAT":
                acc.current_positions[p.instrument] = p
        active_count = len(acc.current_positions)
        logger.info("Cuenta %s reconciliada con %d posiciones activas", account_name, active_count)

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
            # Si es cuenta eval y alcanza el consistency_cap_pct, marcar aplanado obligatorio
            if (
                acc.preset.phase == "eval"
                and acc.preset.consistency_cap_pct
                and acc.preset.profit_target
                and acc.preset.lock_day_after_consistency_cap
            ):
                cap = acc.preset.profit_target * acc.preset.consistency_cap_pct
                if acc.realized_pnl_today >= cap:
                    acc.is_flattened = True
                    logger.warning(
                        "Cuenta Eval %s: Profit de hoy ($%.2f) alcanzo el tope de 50%% ($%.2f). Marcando aplanado obligatorio.",
                        acc.account_name, acc.realized_pnl_today, cap
                    )

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

        # 4. Horarios de sesión: Operación 24hs con corte diario de liquidación/mantenimiento CME (3:00 - 4:00 PM CST)
        now_cme = current_time.astimezone(self.cme_tz) if current_time is not None else datetime.now(self.cme_tz)
        session_cfg = self.config.session

        if session_cfg.mode == "24h_with_break":
            weekday = now_cme.weekday()  # 0=Lunes, 4=Viernes, 5=Sábado, 6=Domingo
            t_cme = now_cme.time()
            break_start = time.fromisoformat(session_cfg.daily_break_start)
            break_end = time.fromisoformat(session_cfg.daily_break_end)

            # Buffer de pre-cierre (ej. 5 min antes: 14:55 CST / 15:55 ET)
            dummy_dt = datetime(2026, 1, 1, break_start.hour, break_start.minute, break_start.second)
            buffered_start = (dummy_dt - timedelta(minutes=session_cfg.pre_break_buffer_minutes)).time()

            # Fin de semana (Viernes post-corte 15:00 CST hasta Domingo reapertura 17:00 CST)
            if weekday == 4 and t_cme >= buffered_start:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"WEEKEND_CLOSED: Mercado CME cerrado por fin de semana desde Viernes {buffered_start.strftime('%H:%M')} CST",
                )
            if weekday == 5:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason="WEEKEND_CLOSED: Mercado CME cerrado los sabados",
                )
            if weekday == 6 and t_cme < time(17, 0):
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason="WEEKEND_CLOSED: Mercado CME reabre Domingos a las 17:00 CST (18:00 ET)",
                )

            # Lunes a Jueves: Break diario de liquidación y mantenimiento (15:00 a 16:00 CST)
            if buffered_start <= t_cme < break_end:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"CME_DAILY_BREAK: Mercado en break diario de liquidacion/mantenimiento ({buffered_start.strftime('%H:%M')} - {break_end.strftime('%H:%M')} CST). Reapertura Globex a las {break_end.strftime('%H:%M')} CST.",
                )
        else:
            # Modo RTH tradicional
            t_now = now_cme.time()
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

        # 7a. Regla de consistencia del 50% de Lucid para Eval (Criterio 9)
        if (
            preset.phase == "eval"
            and preset.consistency_cap_pct
            and preset.profit_target
            and preset.lock_day_after_consistency_cap
        ):
            max_single_day_profit = preset.profit_target * preset.consistency_cap_pct
            if acc.realized_pnl_today >= max_single_day_profit:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"CONSISTENCY_CAP_REACHED: Profit de hoy (${acc.realized_pnl_today:.2f}) alcanzo el tope de consistencia de {int(preset.consistency_cap_pct*100)}% (${max_single_day_profit:.2f}). Entradas bloqueadas para proteger la aprobacion de la cuenta.",
                )

        # 7b. Requisito de dias de ganancia minima en cuentas Funded (min_days_of_profit)
        if (
            preset.phase == "funded"
            and preset.lock_day_after_qualifying_profit
            and preset.min_profit_day_amount
        ):
            if acc.realized_pnl_today >= preset.min_profit_day_amount:
                qualified_so_far = len(acc.qualifying_days_history) + 1
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"FUNDED_QUALIFYING_DAY_LOCKED: Profit de hoy (${acc.realized_pnl_today:.2f}) ya supero el minimo requerido (${preset.min_profit_day_amount:.2f}). Entradas bloqueadas para asegurar el dia calificado para payout ({qualified_so_far}/{preset.min_profit_days_required} dias).",
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
