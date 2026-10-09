"""Motor de riesgo en Python para cuentas de fondeo Lucid Trading.
Evalua cada solicitud de orden contra las reglas de la cuenta (EOD Trailing DD, DLL,
limite de contratos, consistencia, horarios y stop obligatorio).
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from typing import Any, Dict, List, Optional, Tuple
import zoneinfo

from src.gate.config import GateConfig, AccountPreset
from src.gate.rules import liquidation_floor, payout_progress, trading_day_for
from src.gate.state_store import StateStore

logger = logging.getLogger("gate.risk_engine")

MAX_CLOSED_DAYS_KEPT = 90  # historial de dias cerrados que se conserva por cuenta


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
    trading_day: Optional[date] = None  # Etiqueta del dia de trading en curso (fecha en que cierra, 16:00 CT)
    baseline_set: bool = False  # True cuando day_start_balance/eod_hwm vienen de un balance real o del estado persistido
    closed_days: List[Dict[str, Any]] = field(default_factory=list)


class RiskEngine:
    def __init__(self, config: GateConfig, state_store: Optional[StateStore] = None):
        self.config = config
        self.accounts: Dict[str, AccountState] = {}
        self.strategy_paused: Dict[str, bool] = {}
        self.cme_tz = zoneinfo.ZoneInfo(config.session.timezone)
        self.store = state_store
        self._persisted: Dict[str, Any] = state_store.load() if state_store else {}

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
            baseline_set=balance is not None,
        )
        self._restore_state(state)
        self.accounts[account_name] = state
        self._persist()
        return state

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------
    def _serialize(self, acc: AccountState) -> Dict[str, Any]:
        return {
            "preset_id": self.config.accounts.get(acc.account_name),
            "trading_day": acc.trading_day.isoformat() if acc.trading_day else None,
            "day_start_balance": acc.day_start_balance,
            "eod_hwm": acc.eod_hwm,
            "current_balance": acc.current_balance,
            "realized_pnl_today": acc.realized_pnl_today,
            "qualifying_days_history": list(acc.qualifying_days_history),
            "closed_days": list(acc.closed_days),
        }

    def _restore_state(self, acc: AccountState) -> bool:
        saved = self._persisted.get(acc.account_name)
        if not saved:
            return False
        preset_id = self.config.accounts.get(acc.account_name)
        if saved.get("preset_id") != preset_id:
            logger.warning(
                "Cuenta %s: el estado persistido es del preset '%s' y ahora es '%s'. Se descarta.",
                acc.account_name, saved.get("preset_id"), preset_id,
            )
            return False
        try:
            values = {
                "trading_day": date.fromisoformat(saved["trading_day"]) if saved.get("trading_day") else None,
                "day_start_balance": float(saved["day_start_balance"]),
                "eod_hwm": float(saved["eod_hwm"]),
                "current_balance": float(saved["current_balance"]),
                "realized_pnl_today": float(saved.get("realized_pnl_today", 0.0)),
                "qualifying_days_history": [float(v) for v in saved.get("qualifying_days_history", [])],
                "closed_days": list(saved.get("closed_days", [])),
            }
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Cuenta %s: estado persistido invalido (%s). Se descarta.", acc.account_name, exc)
            return False
        for name, value in values.items():
            setattr(acc, name, value)
        acc.baseline_set = True
        logger.info(
            "Cuenta %s: estado restaurado (dia %s, HWM %.2f, %d dias calificados)",
            acc.account_name, acc.trading_day, acc.eod_hwm, len(acc.qualifying_days_history),
        )
        return True

    def _persist(self) -> None:
        if not self.store:
            return
        # Se fusiona con lo ya persistido para no perder cuentas que aun no se registraron en esta ejecucion
        merged = dict(self._persisted)
        merged.update({name: self._serialize(acc) for name, acc in self.accounts.items()})
        try:
            self.store.save(merged)
            self._persisted = merged
        except OSError as exc:
            logger.error("No se pudo persistir el estado del gate: %s", exc)

    # ------------------------------------------------------------------
    # Dia de trading (16:00 CT -> 16:00 CT)
    # ------------------------------------------------------------------
    def trading_day_label(self, moment: datetime) -> date:
        """Fecha en la que cierra el dia de trading que contiene `moment` (corte 16:00 CT = 17:00 ET)."""
        return trading_day_for(moment, self.cme_tz, time.fromisoformat(self.config.session.trading_day_rollover))

    def _roll_day_if_needed(self, acc: AccountState, moment: Optional[datetime]) -> Optional[date]:
        label = self.trading_day_label(moment or datetime.now(self.cme_tz))
        if acc.trading_day is None:
            acc.trading_day = label
            return None
        if label <= acc.trading_day:
            return None
        closed_label = acc.trading_day
        self.close_trading_day(acc.account_name, closed_label)
        acc.trading_day = label
        self._persist()
        return closed_label

    def close_trading_day(self, account_name: str, closed_label: Optional[date] = None) -> Optional[Dict[str, Any]]:
        """Cierra el dia en curso: registra el PnL (record_closed_day), sube el HWM EOD y reinicia el dia."""
        acc = self.accounts.get(account_name)
        if not acc:
            return None

        day_profit = acc.realized_pnl_today
        qualified = self.record_closed_day(account_name, day_profit)
        label = closed_label or acc.trading_day

        entry: Dict[str, Any] = {
            "date": label.isoformat() if label else None,
            "pnl": day_profit,
            "closing_balance": acc.current_balance,
            "qualified": qualified,
        }
        # La etiqueta "sabado" (entre el cierre del viernes y la reapertura del domingo) no es un dia de trading
        if label is not None and label.weekday() < 5:
            acc.closed_days.append(entry)
            del acc.closed_days[:-MAX_CLOSED_DAYS_KEPT]

        acc.eod_hwm = max(acc.eod_hwm, acc.current_balance)  # trailing EOD: el HWM solo sube al cerrar el dia
        acc.day_start_balance = acc.current_balance
        acc.realized_pnl_today = 0.0
        acc.is_flattened = False
        logger.info(
            "Cuenta %s: dia %s cerrado. PnL $%.2f, balance $%.2f, HWM EOD $%.2f%s",
            account_name, label, day_profit, acc.current_balance, acc.eod_hwm, " (dia calificado)" if qualified else "",
        )
        self._persist()
        return entry

    def tick(self, now: Optional[datetime] = None) -> List[Tuple[str, date]]:
        """Cierra los dias vencidos de todas las cuentas. Lo llama el servidor periodicamente."""
        closed: List[Tuple[str, date]] = []
        for acc in list(self.accounts.values()):
            label = self._roll_day_if_needed(acc, now)
            if label is not None:
                closed.append((acc.account_name, label))
        return closed

    def record_closed_day(self, account_name: str, day_profit: float) -> bool:
        """Registra el profit final de una jornada. Si cumple con el minimo de dia ganador en Funded, lo suma al historial."""
        acc = self.accounts.get(account_name)
        if not acc:
            return False
        
        min_amount = acc.preset.min_profit_day_amount or 0.0
        if acc.preset.is_funded_like and day_profit >= min_amount and min_amount > 0:
            acc.qualifying_days_history.append(day_profit)
            logger.info(
                "Cuenta %s: Dia calificado registrado ($%.2f >= $%.2f). Total calificados: %d/%d",
                account_name, day_profit, min_amount, len(acc.qualifying_days_history), acc.preset.min_profit_days_required
            )
            self._persist()
            return True
        return False

    @staticmethod
    def _is_today_qualifying(acc: AccountState) -> bool:
        amount = acc.preset.min_profit_day_amount
        return amount is not None and acc.realized_pnl_today >= amount

    @staticmethod
    def _has_balance_for_payout(acc: AccountState) -> bool:
        target = acc.preset.balance_for_payout
        return target is not None and acc.current_balance >= target

    def get_payout_status(self, account_name: str) -> Dict[str, object]:
        """Estado de elegibilidad de payout (solo Funded).

        Elegible = dias calificados (historicos + hoy) >= min_profit_days_required
                   Y balance actual >= balance_for_payout (initial_balance + buffer + payout).
        mode indica el objetivo vigente:
          - accumulate_balance: aun no se alcanza balance_for_payout (maximizar ganancia).
          - qualify_days: balance_for_payout alcanzado; faltan dias, cada uno requiere min_profit_day_amount.
          - eligible: se cumplen ambas condiciones.
          - payout_not_configured: faltan buffer/payout en la configuracion.
        """
        acc = self.accounts.get(account_name)
        if not acc:
            return {"error": f"Cuenta {account_name} no registrada"}

        preset = acc.preset
        if not preset.is_funded_like:
            return {"account": account_name, "phase": preset.phase, "payout_applicable": False}

        progress = payout_progress(
            preset, acc.current_balance, acc.realized_pnl_today, len(acc.qualifying_days_history)
        )
        return {
            "account": account_name,
            "phase": preset.phase,
            "payout_applicable": True,
            "current_balance": acc.current_balance,
            "initial_balance": preset.initial_balance,
            "buffer": preset.buffer,
            "payout": preset.payout,
            "realized_pnl_today": acc.realized_pnl_today,
            "min_profit_day_amount": preset.min_profit_day_amount,
            **progress,
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

    def _enforce_consistency_cap(self, acc: AccountState) -> None:
        """Eval: si el profit de hoy alcanza consistency_cap_pct del target, marca aplanado obligatorio."""
        preset = acc.preset
        if not (
            preset.phase == "eval"
            and preset.consistency_cap_pct
            and preset.profit_target
            and preset.lock_day_after_consistency_cap
        ):
            return
        cap = preset.profit_target * preset.consistency_cap_pct
        if acc.realized_pnl_today >= cap:
            acc.is_flattened = True
            logger.warning(
                "Cuenta Eval %s: Profit de hoy ($%.2f) alcanzo el tope de 50%% ($%.2f). Marcando aplanado obligatorio.",
                acc.account_name, acc.realized_pnl_today, cap,
            )

    def update_telemetry(self, t: TelemetryUpdate, now: Optional[datetime] = None) -> None:
        acc = self.accounts.get(t.account)
        if not acc:
            acc = self.register_account(t.account)

        self._roll_day_if_needed(acc, now)

        if t.current_balance is not None:
            if not acc.baseline_set:
                # Primer balance real de la cuenta: fija el baseline del dia y del HWM EOD
                acc.day_start_balance = t.current_balance
                acc.eod_hwm = t.current_balance
                acc.baseline_set = True
            acc.current_balance = t.current_balance

        # PnL del dia: el valor explicito manda; si no, se deriva del balance (balance - balance de inicio del dia)
        if t.realized_pnl_today is not None:
            acc.realized_pnl_today = t.realized_pnl_today
        elif t.current_balance is not None:
            acc.realized_pnl_today = acc.current_balance - acc.day_start_balance
        if t.realized_pnl_today is not None or t.current_balance is not None:
            self._enforce_consistency_cap(acc)

        if t.unrealized_pnl is not None:
            acc.unrealized_pnl = t.unrealized_pnl

        if t.positions is not None:
            acc.current_positions.clear()
            for p in t.positions:
                acc.current_positions[p.instrument] = p

        self._persist()

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
        self._roll_day_if_needed(acc, current_time)  # cierra el dia anterior si ya cruzo 16:00 CT

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

        # 4. Horarios de sesión (hora de Chicago, la oficial del CME; sigue el DST de Chicago).
        # 24h con break diario de mantenimiento 16:00-17:00 CT (17:00-18:00 ET); cierre semanal V 16:00 CT
        # y reapertura domingo 17:00 CT.
        now_cme = current_time.astimezone(self.cme_tz) if current_time is not None else datetime.now(self.cme_tz)
        session_cfg = self.config.session

        if session_cfg.mode == "24h_with_break":
            weekday = now_cme.weekday()  # 0=Lunes, 4=Viernes, 5=Sábado, 6=Domingo
            t_cme = now_cme.time()
            break_start = time.fromisoformat(session_cfg.daily_break_start)
            break_end = time.fromisoformat(session_cfg.daily_break_end)

            # Buffer de pre-cierre (5 min antes del break: 15:55 CT)
            dummy_dt = datetime(2026, 1, 1, break_start.hour, break_start.minute, break_start.second)
            buffered_start = (dummy_dt - timedelta(minutes=session_cfg.pre_break_buffer_minutes)).time()

            # Fin de semana: desde el viernes (buffer previo al cierre) hasta la reapertura del domingo
            if weekday == 4 and t_cme >= buffered_start:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"WEEKEND_CLOSED: Mercado CME cerrado por fin de semana desde el viernes {buffered_start.strftime('%H:%M')} CT",
                )
            if weekday == 5:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason="WEEKEND_CLOSED: Mercado CME cerrado los sabados",
                )
            if weekday == 6 and t_cme < break_end:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"WEEKEND_CLOSED: Mercado CME reabre el domingo a las {break_end.strftime('%H:%M')} CT",
                )

            # Break diario de mantenimiento (incluye el buffer previo)
            if buffered_start <= t_cme < break_end:
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"CME_DAILY_BREAK: Entradas bloqueadas por break diario del CME ({buffered_start.strftime('%H:%M')} - {break_end.strftime('%H:%M')} CT). Reapertura Globex a las {break_end.strftime('%H:%M')} CT.",
                )
        else:
            # Modo RTH tradicional (horas de preset en hora de Chicago)
            t_now = now_cme.time()
            start_t = time.fromisoformat(preset.session_start_time)
            flatten_t = time.fromisoformat(preset.session_flatten_time)
            if not (start_t <= t_now < flatten_t):
                return AuthResponse(
                    request_id=req.request_id,
                    allow=False,
                    max_qty=0,
                    reason=f"OUTSIDE_TRADING_HOURS: Hora actual {t_now.strftime('%H:%M')} fuera del rango permitido ({preset.session_start_time} - {preset.session_flatten_time} CT)",
                )

        # 5. Drawdown EOD y piso de liquidación (Criterio 9)
        current_equity = acc.current_balance + acc.unrealized_pnl
        effective_floor = liquidation_floor(preset, acc.eod_hwm)

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

        # 7b. Funded: objetivo = llegar a balance_for_payout lo antes posible; NO se bloquea el dia mientras
        # no se alcance. Una vez alcanzado, los dias restantes necesitan solo min_profit_day_amount: al
        # lograrlo se bloquea el dia para asegurarlo (mientras falten dias calificados historicos).
        if (
            preset.is_funded_like
            and preset.lock_day_after_qualifying_profit
            and preset.min_profit_day_amount
            and self._has_balance_for_payout(acc)
            and self._is_today_qualifying(acc)
            and len(acc.qualifying_days_history) < preset.min_profit_days_required
        ):
            qualified_so_far = len(acc.qualifying_days_history) + 1
            return AuthResponse(
                request_id=req.request_id,
                allow=False,
                max_qty=0,
                reason=(
                    f"FUNDED_QUALIFYING_DAY_LOCKED: balance_for_payout alcanzado "
                    f"(${acc.current_balance:.2f} >= ${preset.balance_for_payout:.2f}) y el profit de hoy "
                    f"(${acc.realized_pnl_today:.2f}) ya cumple min_profit_day_amount "
                    f"(${preset.min_profit_day_amount:.2f}). Entradas bloqueadas para asegurar el dia calificado "
                    f"({qualified_so_far}/{preset.min_profit_days_required} dias)."
                ),
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
