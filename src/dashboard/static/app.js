"use strict";

const REFRESH_SECONDS = 300;
const MODE_TEXT = {
  accumulate_balance: "Acumulando balance hacia el payout",
  qualify_days: "Balance de payout alcanzado: cada día restante requiere el mínimo de ganancia",
  eligible: "Elegible para payout",
  payout_not_configured: "Payout sin configurar (buffer/payout)",
};
const PHASE_TEXT = { eval: "Eval", funded: "Funded", payout: "Payout" };
const RISK = { ok: ["badge-ok", "En orden"], warn: ["badge-warn", "Atención"], danger: ["badge-danger", "Crítico"], unknown: ["badge-muted", "Sin preset"] };

const els = {
  account: document.getElementById("filter-account"), day: document.getElementById("filter-day"),
  refresh: document.getElementById("btn-refresh"), auto: document.getElementById("auto-refresh"),
  info: document.getElementById("refresh-info"), banner: document.getElementById("error-banner"),
  pill: document.getElementById("gate-pill"), kpis: document.getElementById("kpis"),
  accounts: document.getElementById("accounts"), closed: document.getElementById("closed-days"),
  decisions: document.getElementById("decisions"), events: document.getElementById("events"),
  chips: document.getElementById("decision-chips"),
  start: document.getElementById("btn-start"), notice: document.getElementById("notice"),
};
const state = { data: null, result: "all", countdown: REFRESH_SECONDS, loading: false, updatedAt: null, csrf: null };

// ---------- helpers (todo el texto dinamico entra por textContent, nunca por innerHTML) ----------
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value; else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
const money = (v, d = 2) => (v === null || v === undefined) ? "-" :
  (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (v) => (v === null || v === undefined) ? "-" : (v * 100).toFixed(0) + "%";
const clamp01 = (v) => Math.max(0, Math.min(1, v));
const clock = (iso) => iso ? iso.slice(11, 19) : "-";          // el ISO ya viene en la zona del gate: no se reconvierte
const stamp = (iso) => iso ? iso.slice(0, 10) + " " + iso.slice(11, 19) : "-";
const signClass = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
function ago(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 90) return "hace " + Math.round(seconds) + " s";
  if (seconds < 5400) return "hace " + Math.round(seconds / 60) + " min";
  if (seconds < 172800) return "hace " + (seconds / 3600).toFixed(1) + " h";
  return "hace " + Math.round(seconds / 86400) + " días";
}
function table(target, headers, rows, emptyText) {
  target.replaceChildren();
  if (!rows.length) { target.append(h("tbody", {}, h("tr", {}, h("td", { class: "empty" }, emptyText)))); return; }
  target.append(
    h("thead", {}, h("tr", {}, headers.map(([label, cls]) => h("th", { class: cls }, label)))),
    h("tbody", {}, rows),
  );
}
function meter(label, valueText, fraction, tone) {
  return h("div", { class: "meter" },
    h("div", { class: "row" }, h("span", {}, label), h("span", {}, valueText)),
    h("div", { class: "bar " + (tone || "") }, h("span", { style: "width:" + (clamp01(fraction) * 100).toFixed(1) + "%" })));
}

// ---------- render ----------
function renderStart(data) {
  // Solo se ofrece cuando el dashboard puede lanzarlo (loopback) y el puerto del gate realmente no escucha
  state.csrf = data.meta.csrf_token || null;
  els.start.hidden = !(data.meta.start_enabled && data.gate.listening === false);
}

function showNotice(message, tail, isError) {
  els.notice.className = "banner notice" + (isError ? " error" : "");
  els.notice.replaceChildren(h("div", {}, message), tail && tail.length ? h("pre", {}, tail.join("\n")) : null);
  els.notice.hidden = false;
}

async function startGate() {
  if (!state.csrf || !confirm("¿Arrancar el gate de autorización de Python en este servidor?")) return;
  els.start.disabled = true;
  els.start.textContent = "Arrancando…";
  try {
    const response = await fetch("/api/gate/start", { method: "POST", headers: { "X-Dashboard-Token": state.csrf } });
    const result = await response.json();
    showNotice(result.message || "Sin respuesta", result.console_tail, !result.ok);
  } catch (error) {
    showNotice("No se pudo contactar al dashboard: " + error.message, null, true);
  } finally {
    els.start.disabled = false;
    els.start.textContent = "Arrancar gate";
    await load();
  }
}

function renderPill(gate) {
  const map = { running: ["pill-ok", "Gate en línea"], stopped: ["pill-danger", "Gate detenido"],
                unknown: ["pill-warn", "Estado desconocido"], no_data: ["pill-muted", "Sin datos de auditoría"] };
  const [cls, text] = map[gate.status] || map.unknown;
  els.pill.className = "pill " + cls;
  els.pill.textContent = text;
}

function renderKpis(data) {
  const { gate, summary } = data, d = summary.decisions;
  const reasons = d.top_reject_reasons.length
    ? d.top_reject_reasons.slice(0, 3).map(r => h("div", { class: "sub" }, r.code + " · " + r.count))
    : [h("div", { class: "sub" }, "Sin rechazos")];
  const kpi = (label, value, ...sub) => h("div", { class: "kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub);
  els.kpis.replaceChildren(
    kpi("Gate", ({ running: "En línea", stopped: "Detenido", unknown: "Desconocido", no_data: "Sin datos" })[gate.status] || "-",
        h("div", { class: "sub" }, "Clientes NT8 conectados: " + gate.connected_clients),
        h("div", { class: "sub" }, "Último evento " + ago(gate.last_event_age_seconds))),
    kpi("Decisiones", d.total,
        h("div", { class: "sub" }, d.allowed + " aprobadas · " + d.denied + " rechazadas · " + d.no_response + " sin respuesta")),
    kpi("Tasa de aprobación", pct(d.approval_rate), h("div", { class: "sub" }, "Sobre decisiones respondidas")),
    kpi("Latencia de decisión (servidor)", d.latency_ms_avg === null ? "-" : d.latency_ms_avg + " ms",
        h("div", { class: "sub" }, "p95 " + (d.latency_ms_p95 ?? "-") + " ms · máx " + (d.latency_ms_max ?? "-") + " ms")),
    kpi("Motivos de rechazo", d.denied, reasons),
    kpi("Telemetrías recibidas", summary.events.telemetry,
        h("div", { class: "sub" }, summary.events.reconciles + " reconciliaciones · " + summary.events.commands + " comandos · " + summary.events.days_closed + " cierres de día")),
  );
}

function renderAccount(a) {
  const [riskClass, riskText] = RISK[a.risk] || RISK.unknown;
  const tags = [h("span", { class: "badge " + riskClass }, riskText)];
  if (a.phase) tags.unshift(h("span", { class: "badge badge-info" }, (PHASE_TEXT[a.phase] || a.phase) + " " + (a.tier || "")));
  if (a.pending_day_close) tags.push(h("span", { class: "badge badge-warn", title: "El estado es de un día anterior; el gate lo cierra en el próximo tick" }, "Cierre de día pendiente"));

  const card = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("span", { class: "name" }, a.account), h("span", { class: "tags" }, tags)),
    h("div", { class: "stats" },
      h("div", {}, h("div", { class: "label" }, "Balance"), h("div", { class: "value" }, money(a.balance))),
      h("div", {}, h("div", { class: "label" }, "PnL del día (" + (a.state_trading_day || "-") + ")"), h("div", { class: "value " + signClass(a.pnl_today) }, money(a.pnl_today))),
    ));
  if (!a.preset_known) {
    card.append(h("div", { class: "note" }, "Preset '" + (a.preset_id || "?") + "' no existe en la configuración: sin métricas de riesgo."));
    return card;
  }
  const tone = (frac) => frac > 0.5 ? "ok" : frac > 0.25 ? "warn" : "danger";
  const cushionFrac = a.drawdown_cushion / a.max_loss_limit;
  const dllFrac = a.dll_remaining / a.daily_loss_limit;
  card.append(
    meter("Colchón de drawdown", money(a.drawdown_cushion, 0) + " sobre el piso " + money(a.liquidation_floor, 0), cushionFrac, tone(cushionFrac)),
    meter("Pérdida diaria restante (DLL)", money(Math.max(0, a.dll_remaining), 0) + " de " + money(a.daily_loss_limit, 0), dllFrac, tone(dllFrac)),
  );
  if (a.eval) {
    const e = a.eval;
    card.append(meter("Progreso al profit target", money(e.profit, 0) + " de " + money(e.profit_target, 0), e.profit_target ? e.profit / e.profit_target : 0, ""));
    if (e.consistency_cap) card.append(h("div", { class: "note" },
      "Consistencia: hoy " + money(a.pnl_today, 0) + " de un tope de " + money(e.consistency_cap, 0) + " (" + pct(e.consistency_pct) + " del profit acumulado, con piso del " + pct(e.consistency_pct) + " del target). " +
      "Mejor día / profit total: " + pct(e.best_day_share) + " (informativo)."));
  }
  if (a.payout) {
    const p = a.payout, span = (p.balance_for_payout || 0) - a.initial_balance;
    if (p.payout_configured && span > 0) card.append(meter("Balance hacia el payout",
      money(a.balance, 0) + " de " + money(p.balance_for_payout, 0), (a.balance - a.initial_balance) / span, ""));
    const filled = Math.min(p.qualified_days_count, p.required_qualifying_days);
    card.append(h("div", { class: "meter" },
      h("div", { class: "row" }, h("span", {}, "Días calificados (≥ " + money(p.min_profit_day_amount, 0) + ")"), h("span", {}, p.qualified_days_count + " / " + p.required_qualifying_days)),
      h("div", { class: "dots", "aria-hidden": "true" }, "●".repeat(filled) + "○".repeat(Math.max(0, p.required_qualifying_days - filled)))));
    const detail = p.mode === "accumulate_balance" ? " · faltan " + money(p.balance_remaining, 0) : "";
    card.append(h("div", { class: "note" }, (MODE_TEXT[p.mode] || p.mode) + detail));
  }
  return card;
}

function renderClosedDays(data) {
  const rows = data.closed_days.map(d => h("tr", {},
    h("td", {}, d.date), h("td", {}, d.account),
    h("td", { class: "num " + signClass(d.pnl) }, money(d.pnl)), h("td", { class: "num" }, money(d.closing_balance)),
    h("td", {}, h("span", { class: "badge " + (d.qualified ? "badge-ok" : "badge-muted") }, d.qualified ? "Calificado" : "-"))));
  table(els.closed, [["Día"], ["Cuenta"], ["PnL", "num"], ["Balance de cierre", "num"], ["Día calificado"]], rows, "Sin días cerrados para este filtro.");
}

function resultBadge(allow) {
  return allow === true ? h("span", { class: "badge badge-ok" }, "Aprobada")
    : allow === false ? h("span", { class: "badge badge-danger" }, "Rechazada")
    : h("span", { class: "badge badge-warn" }, "Sin respuesta");
}

function renderDecisions(data) {
  const keep = { all: () => true, allow: d => d.allow === true, deny: d => d.allow === false, none: d => d.allow === null };
  const shown = data.decisions.filter(keep[state.result]);
  const rows = shown.map(d => h("tr", {},
    h("td", {}, clock(d.ts)), h("td", {}, d.trading_day), h("td", {}, d.account), h("td", {}, d.strategy_id),
    h("td", {}, d.instrument), h("td", {}, d.side), h("td", { class: "num" }, d.qty ?? "-"), h("td", { class: "num" }, d.stop_distance ?? "-"),
    h("td", {}, resultBadge(d.allow)), h("td", { class: "wrap" }, d.reason || "-"),
    h("td", { class: "num" }, d.latency_ms ?? "-")));
  table(els.decisions,
    [["Hora"], ["Día"], ["Cuenta"], ["Estrategia"], ["Instrumento"], ["Lado"], ["Qty", "num"], ["Stop", "num"], ["Resultado"], ["Motivo"], ["Latencia ms", "num"]],
    rows, "Sin decisiones para este filtro.");
}

function renderEvents(data) {
  const rows = data.events.map(e => h("tr", {}, h("td", {}, stamp(e.ts)), h("td", {}, e.event), h("td", {}, e.account || "-"), h("td", { class: "wrap" }, e.detail || "")));
  table(els.events, [["Fecha y hora"], ["Evento"], ["Cuenta"], ["Detalle"]], rows, "Sin eventos para este filtro.");
}

function fillSelect(select, values, current, allLabel) {
  select.replaceChildren(h("option", { value: "" }, allLabel), ...values.map(v => h("option", { value: v }, v)));
  select.value = values.includes(current) ? current : "";
}

function render(data) {
  state.data = data;
  fillSelect(els.account, data.meta.accounts, data.meta.filters.account || els.account.value, "Todas");
  fillSelect(els.day, data.meta.days, data.meta.filters.day || els.day.value, "Todos");
  renderPill(data.gate);
  renderStart(data);
  renderKpis(data);
  els.accounts.replaceChildren(...(data.accounts.length ? data.accounts.map(renderAccount)
    : [h("div", { class: "empty" }, data.meta.state_file_found ? "Sin cuentas para este filtro." : "Aún no existe state/gate_state.json (se crea cuando NT8 reporta la primera cuenta).")]));
  renderClosedDays(data);
  renderDecisions(data);
  renderEvents(data);
}

// ---------- carga y auto-refresh ----------
async function load() {
  if (state.loading) return;
  state.loading = true;
  els.refresh.disabled = true;
  const params = new URLSearchParams();
  if (els.account.value) params.set("account", els.account.value);
  if (els.day.value) params.set("day", els.day.value);
  try {
    const response = await fetch("/api/overview?" + params.toString(), { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || response.statusText);
    els.banner.hidden = true;
    render(payload);
    state.updatedAt = new Date();
    history.replaceState(null, "", params.toString() ? "?" + params.toString() : location.pathname);
  } catch (error) {
    els.banner.textContent = "No se pudo actualizar el dashboard: " + error.message + " (se muestran los últimos datos cargados)";
    els.banner.hidden = false;
  } finally {
    state.loading = false;
    els.refresh.disabled = false;
    state.countdown = REFRESH_SECONDS;
    tick();
  }
}

function tick() {
  const updated = state.updatedAt ? "Actualizado " + state.updatedAt.toLocaleTimeString() : "";
  const next = els.auto.checked ? " · próxima en " + Math.floor(state.countdown / 60) + ":" + String(state.countdown % 60).padStart(2, "0") : " · auto-actualización apagada";
  els.info.textContent = updated + next;
}

setInterval(() => {
  if (els.auto.checked && !state.loading) {
    state.countdown -= 1;
    if (state.countdown <= 0) load(); else tick();
  }
}, 1000);

els.refresh.addEventListener("click", load);
els.start.addEventListener("click", startGate);
els.account.addEventListener("change", load);
els.day.addEventListener("change", load);
els.auto.addEventListener("change", () => { localStorage.setItem("gate-dashboard-auto", els.auto.checked ? "1" : "0"); state.countdown = REFRESH_SECONDS; tick(); });
els.chips.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-result]");
  if (!button) return;
  state.result = button.dataset.result;
  els.chips.querySelectorAll(".chip").forEach(c => c.classList.toggle("active", c === button));
  if (state.data) renderDecisions(state.data);
});

// filtros iniciales desde la URL (enlaces compartibles) y preferencia de auto-refresh
const initial = new URLSearchParams(location.search);
els.auto.checked = localStorage.getItem("gate-dashboard-auto") !== "0";
if (initial.get("account")) els.account.append(h("option", { value: initial.get("account") }, initial.get("account")));
if (initial.get("day")) els.day.append(h("option", { value: initial.get("day") }, initial.get("day")));
els.account.value = initial.get("account") || "";
els.day.value = initial.get("day") || "";
load();
