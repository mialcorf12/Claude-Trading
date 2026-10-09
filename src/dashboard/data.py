"""Capa de datos del dashboard: lee state/gate_state.json y logs/gate_audit.log (solo lectura).

No importa el motor de riesgo en ejecucion: reutiliza solo las formulas puras de `src.gate.rules`,
asi el dashboard y el gate calculan piso de liquidacion y progreso de payout con la misma logica.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import zoneinfo

from src.gate.config import AccountPreset, GateConfig
from src.gate.rules import liquidation_floor, payout_progress, trading_day_for

DEFAULT_MAX_LOG_BYTES = 20 * 1024 * 1024
MAX_ROWS = 300  # filas maximas de decisiones/eventos devueltas al navegador (las mas recientes)
WARN_CUSHION_FRACTION = 0.30  # riesgo "warn" si el colchon de drawdown baja del 30% del max loss
WARN_DLL_FRACTION = 0.70      # ... o si el DLL usado supera el 70%

Record = Dict[str, Any]


@dataclass(frozen=True)
class DashboardSources:
    state_path: Path
    audit_path: Path
    config: GateConfig
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES


# ----------------------------------------------------------------------
# Lectura de archivos
# ----------------------------------------------------------------------
def read_state(path: Path) -> Tuple[Dict[str, Any], Optional[datetime]]:
    """Devuelve ({cuenta: estado}, mtime) o ({}, None) si no existe o es ilegible."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        accounts = data.get("accounts", {})
        mtime = datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone()
        return (accounts if isinstance(accounts, dict) else {}), mtime
    except (OSError, ValueError):
        return {}, None


def read_audit(path: Path, max_bytes: int = DEFAULT_MAX_LOG_BYTES) -> Tuple[List[Record], int]:
    """Lee el log de auditoria (JSON por linea). Si es enorme solo toma la cola. Devuelve (registros, lineas_invalidas)."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # descarta la linea parcial del corte
            raw = handle.read()
    except OSError:
        return [], 0

    records: List[Record] = []
    skipped = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            stamp = item.get("timestamp") or item.get("timestamp_utc")  # el formato viejo usaba timestamp_utc
            records.append({
                "ts": datetime.fromisoformat(stamp),
                "event": str(item["event"]),
                "data": item.get("data") if isinstance(item.get("data"), dict) else {},
            })
        except (ValueError, KeyError, TypeError, AttributeError):
            skipped += 1
    return records, skipped


# ----------------------------------------------------------------------
# Decisiones y eventos
# ----------------------------------------------------------------------
def _reason_code(reason: str) -> str:
    return reason.split(":", 1)[0].strip() if reason else ""


def build_decisions(records: List[Record]) -> List[Record]:
    """Une AUTH_REQUEST_RECEIVED con AUTH_RESPONSE_SENT por request_id. Sin respuesta -> allow = None."""
    pending: Dict[str, Record] = {}
    decisions: List[Record] = []
    for record in records:
        data = record["data"]
        if record["event"] == "AUTH_REQUEST_RECEIVED":
            row = {
                "ts": record["ts"], "request_id": str(data.get("request_id", "")),
                "account": data.get("account", ""), "strategy_id": data.get("strategy_id", ""),
                "instrument": data.get("instrument", ""), "side": data.get("side", ""),
                "qty": data.get("qty"), "stop_distance": data.get("stop_distance"),
                "allow": None, "max_qty": None, "reason": "", "reason_code": "", "latency_ms": None,
            }
            pending[row["request_id"]] = row
            decisions.append(row)
        elif record["event"] == "AUTH_RESPONSE_SENT":
            row = pending.get(str(data.get("request_id", "")))
            if row is not None:
                reason = str(data.get("reason", ""))
                row.update(
                    allow=bool(data.get("allow")), max_qty=data.get("max_qty"), reason=reason,
                    reason_code=_reason_code(reason),
                    latency_ms=round((record["ts"] - row["ts"]).total_seconds() * 1000, 1),
                )
    return decisions


def summarize_decisions(decisions: List[Record]) -> Dict[str, Any]:
    allowed = sum(1 for d in decisions if d["allow"] is True)
    denied = sum(1 for d in decisions if d["allow"] is False)
    answered = allowed + denied
    latencies = sorted(d["latency_ms"] for d in decisions if d["latency_ms"] is not None)
    reasons = Counter(d["reason_code"] for d in decisions if d["allow"] is False and d["reason_code"])
    return {
        "total": len(decisions),
        "allowed": allowed,
        "denied": denied,
        "no_response": len(decisions) - answered,
        "approval_rate": (allowed / answered) if answered else None,
        "latency_ms_avg": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "latency_ms_p95": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None,
        "latency_ms_max": latencies[-1] if latencies else None,
        "top_reject_reasons": [{"code": code, "count": count} for code, count in reasons.most_common(5)],
    }


EVENT_LABELS = {"AUTH_REQUEST_RECEIVED", "AUTH_RESPONSE_SENT"}  # ya cubiertos por la tabla de decisiones


def _event_detail(event: str, data: Dict[str, Any]) -> str:
    if event == "TELEMETRY_RECEIVED":
        parts = []
        if data.get("current_balance") is not None:
            parts.append(f"balance {float(data['current_balance']):.2f}")
        if data.get("unrealized_pnl") is not None:
            parts.append(f"no realizado {float(data['unrealized_pnl']):.2f}")
        return " · ".join(parts)
    if event == "RECONCILE_RECEIVED":
        return f"{len(data.get('positions', []))} posiciones abiertas"
    if event == "COMMAND_BROADCAST":
        target = data.get("account") or data.get("strategy_id") or ""
        return f"{data.get('action', '')} {target}".strip()
    if event == "TRADING_DAY_CLOSED":
        return f"dia {data.get('day', '')} cerrado"
    if event in ("CLIENT_CONNECTED", "CLIENT_DISCONNECTED"):
        return str(data.get("peer", ""))
    if event in ("SERVER_START",):
        return f"{data.get('host', '')}:{data.get('port', '')}"
    return ""


def build_events(records: List[Record]) -> List[Record]:
    events = []
    for record in records:
        if record["event"] in EVENT_LABELS:
            continue
        data = record["data"]
        events.append({
            "ts": record["ts"], "event": record["event"], "account": data.get("account", ""),
            "detail": _event_detail(record["event"], data),
        })
    return events


def gate_status(records: List[Record], now: datetime) -> Dict[str, Any]:
    if not records:
        return {"status": "no_data", "connected_clients": 0, "last_event_at": None, "last_event_age_seconds": None}
    last_server = next((r for r in reversed(records) if r["event"] in ("SERVER_START", "SERVER_STOP")), None)
    status = "unknown" if last_server is None else ("running" if last_server["event"] == "SERVER_START" else "stopped")
    since = records
    if last_server is not None and last_server["event"] == "SERVER_START":
        since = records[records.index(last_server):]
    connected = sum(1 for r in since if r["event"] == "CLIENT_CONNECTED") - sum(
        1 for r in since if r["event"] == "CLIENT_DISCONNECTED"
    )
    last = records[-1]["ts"]
    return {
        "status": status,
        "connected_clients": max(0, connected) if status == "running" else 0,
        "last_event_at": last,
        "last_event_age_seconds": (now - last).total_seconds(),
    }


# ----------------------------------------------------------------------
# Metricas por cuenta (snapshot de state/gate_state.json)
# ----------------------------------------------------------------------
def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def account_metrics(name: str, saved: Dict[str, Any], preset: Optional[AccountPreset], current_day: date) -> Dict[str, Any]:
    balance = _num(saved.get("current_balance"))
    pnl_today = _num(saved.get("realized_pnl_today"))
    state_day = saved.get("trading_day")
    history = [_num(v) for v in saved.get("qualifying_days_history", [])]
    closed_days = saved.get("closed_days", [])

    metrics: Dict[str, Any] = {
        "account": name,
        "preset_id": saved.get("preset_id"),
        "preset_known": preset is not None,
        "balance": balance,
        "day_start_balance": _num(saved.get("day_start_balance")),
        "pnl_today": pnl_today,
        "eod_hwm": _num(saved.get("eod_hwm")),
        "state_trading_day": state_day,
        "current_trading_day": current_day.isoformat(),
        "pending_day_close": bool(state_day) and state_day < current_day.isoformat(),
        "qualifying_days_history": history,
        "closed_days_count": len(closed_days),
    }
    if preset is None:
        metrics["risk"] = "unknown"
        return metrics

    floor = liquidation_floor(preset, metrics["eod_hwm"])
    cushion = balance - floor
    dll_used = max(0.0, -pnl_today)
    dll_remaining = preset.daily_loss_limit - dll_used

    if cushion <= 0 or dll_remaining <= 0:
        risk = "danger"
    elif cushion < WARN_CUSHION_FRACTION * preset.max_loss_limit or dll_used >= WARN_DLL_FRACTION * preset.daily_loss_limit:
        risk = "warn"
    else:
        risk = "ok"

    metrics.update(
        tier=preset.tier, phase=preset.phase, initial_balance=preset.initial_balance,
        max_loss_limit=preset.max_loss_limit, liquidation_floor=floor, drawdown_cushion=cushion,
        daily_loss_limit=preset.daily_loss_limit, dll_used=dll_used, dll_remaining=dll_remaining,
        risk=risk,
    )

    if preset.phase == "eval":
        profit = balance - preset.initial_balance
        day_pnls = [_num(d.get("pnl")) for d in closed_days] + [pnl_today]
        best_day = max(day_pnls) if day_pnls else 0.0
        metrics["eval"] = {
            "profit": profit,
            "profit_target": preset.profit_target,
            "consistency_cap": (preset.profit_target * preset.consistency_cap_pct)
            if preset.profit_target and preset.consistency_cap_pct else None,
            "consistency_pct": preset.consistency_cap_pct,
            # Informativo: Lucid evalua el mejor dia contra el profit total acumulado
            "best_day": best_day,
            "best_day_share": (best_day / profit) if profit > 0 else None,
        }
    if preset.is_funded_like:
        progress = payout_progress(preset, balance, pnl_today, len(history))
        metrics["payout"] = {**progress, "buffer": preset.buffer, "payout": preset.payout,
                             "min_profit_day_amount": preset.min_profit_day_amount}
    return metrics


# ----------------------------------------------------------------------
# Resumen completo
# ----------------------------------------------------------------------
def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


def build_overview(
    sources: DashboardSources,
    account: Optional[str] = None,
    day: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    config = sources.config
    tz = zoneinfo.ZoneInfo(config.session.timezone)
    rollover = time.fromisoformat(config.session.trading_day_rollover)
    now = now or datetime.now(tz)
    current_day = trading_day_for(now, tz, rollover)

    state_accounts, state_mtime = read_state(sources.state_path)
    records, skipped = read_audit(sources.audit_path, sources.max_log_bytes)

    decisions = build_decisions(records)
    events = build_events(records)
    for row in decisions + events:
        row["trading_day"] = trading_day_for(row["ts"], tz, rollover).isoformat()

    all_closed = [
        {"account": name, "date": d.get("date"), "pnl": _num(d.get("pnl")),
         "closing_balance": _num(d.get("closing_balance")), "qualified": bool(d.get("qualified"))}
        for name, saved in state_accounts.items() for d in saved.get("closed_days", [])
    ]

    meta_accounts = sorted(
        set(config.accounts) | set(state_accounts)
        | {r["account"] for r in decisions + events if r.get("account")}
    )
    meta_days = sorted(
        {r["trading_day"] for r in decisions + events} | {d["date"] for d in all_closed if d["date"]},
        reverse=True,
    )

    def keep(row: Record, day_key: str) -> bool:
        return (account is None or row.get("account") == account) and (day is None or row.get(day_key) == day)

    decisions_f = [d for d in decisions if keep(d, "trading_day")]
    events_f = [e for e in events if keep(e, "trading_day")]
    closed_f = [c for c in all_closed if keep(c, "date")]

    cards = []
    for name in sorted(state_accounts):
        if account is not None and name != account:
            continue
        saved = state_accounts[name]
        preset_id = saved.get("preset_id") or config.accounts.get(name)
        preset = config.presets.get(preset_id or "")
        cards.append(account_metrics(name, saved, preset, current_day))

    def newest_first(rows: List[Record], limit: int = MAX_ROWS) -> List[Record]:
        return [{**r, "ts": r["ts"].isoformat()} for r in sorted(rows, key=lambda r: r["ts"], reverse=True)[:limit]]

    gate = gate_status(records, now)
    gate["last_event_at"] = _iso(gate["last_event_at"])

    event_counts = Counter(e["event"] for e in events_f)
    return {
        "meta": {
            "generated_at": now.isoformat(),
            "timezone": config.session.timezone,
            "trading_day_rollover": config.session.trading_day_rollover,
            "current_trading_day": current_day.isoformat(),
            "accounts": meta_accounts,
            "days": meta_days,
            "filters": {"account": account, "day": day},
            "state_file_found": bool(state_accounts) or sources.state_path.exists(),
            "state_file_updated_at": _iso(state_mtime),
            "audit_file_found": sources.audit_path.exists(),
            "audit_skipped_lines": skipped,
        },
        "gate": gate,
        "summary": {
            "decisions": summarize_decisions(decisions_f),
            "events": {
                "telemetry": event_counts.get("TELEMETRY_RECEIVED", 0),
                "commands": event_counts.get("COMMAND_BROADCAST", 0),
                "reconciles": event_counts.get("RECONCILE_RECEIVED", 0),
                "days_closed": event_counts.get("TRADING_DAY_CLOSED", 0),
            },
        },
        "accounts": cards,
        "closed_days": sorted(closed_f, key=lambda c: (c["date"] or "", c["account"]), reverse=True),
        "decisions": newest_first(decisions_f),
        "events": newest_first(events_f),
    }
