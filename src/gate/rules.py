"""Formulas puras de las reglas de cuenta, compartidas por el motor de riesgo y el dashboard."""
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Dict, Optional

from src.gate.config import AccountPreset


def liquidation_floor(preset: AccountPreset, eod_hwm: float) -> float:
    """Balance por debajo del cual se pierde la cuenta.

    Eval: min_account_balance es el piso inicial; el trailing EOD (HWM - max loss) lo sube.
    Funded/Payout: el trailing sube con el HWM pero se detiene en min_account_balance.
    """
    trailing_floor = eod_hwm - preset.max_loss_limit
    if preset.is_funded_like:
        return min(trailing_floor, preset.min_account_balance)
    return max(preset.min_account_balance, trailing_floor)


def trading_day_for(moment: datetime, tz: tzinfo, rollover: time) -> date:
    """Fecha en la que cierra el dia de trading que contiene `moment` (corte `rollover` en la zona `tz`)."""
    local = moment.astimezone(tz)
    return local.date() + timedelta(days=1) if local.time() >= rollover else local.date()


def payout_progress(
    preset: AccountPreset,
    current_balance: float,
    realized_today: float,
    historical_qualifying_days: int,
) -> Dict[str, Any]:
    """Progreso hacia el payout (solo Funded/Payout).

    mode: accumulate_balance (aun sin balance_for_payout) | qualify_days (balance alcanzado, faltan dias) |
          eligible | payout_not_configured.
    """
    amount: Optional[float] = preset.min_profit_day_amount
    is_today_qualifying = amount is not None and realized_today >= amount
    qualified_days = historical_qualifying_days + (1 if is_today_qualifying else 0)
    has_min_days = qualified_days >= preset.min_profit_days_required
    target = preset.balance_for_payout
    configured = target is not None
    has_balance = configured and current_balance >= target
    eligible = configured and has_min_days and has_balance

    if not configured:
        mode = "payout_not_configured"
    elif eligible:
        mode = "eligible"
    elif has_balance:
        mode = "qualify_days"
    else:
        mode = "accumulate_balance"

    return {
        "payout_configured": configured,
        "mode": mode,
        "balance_for_payout": target,
        "balance_remaining": max(0.0, target - current_balance) if configured else None,
        "qualified_days_count": qualified_days,
        "required_qualifying_days": preset.min_profit_days_required,
        "days_remaining": max(0, preset.min_profit_days_required - qualified_days),
        "is_today_qualifying": is_today_qualifying,
        "has_min_days": has_min_days,
        "has_balance_for_payout": has_balance,
        "is_payout_eligible": eligible,
    }
