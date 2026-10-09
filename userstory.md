## [TRD-XX] Gate de autorización Python ↔ NinjaScript para Lucid Trading

**Tipo:** Story | **Épica:** Trading Automation – Execution Adapters | **Estimación:** 8 pts

### Historia
Como operador de cuentas de fondeo,
quiero que las estrategias NinjaScript aprobadas operen en vivo en NinjaTrader 8
solo cuando Python las autorice según su estado (aprobada + operativa) y las reglas
de la cuenta Lucid,
para que el código probado en backtesting sea exactamente el que opera en vivo.

### Contexto
- Entorno: VPS Windows, NT8 Desktop, cuenta Lucid.
- NinjaScript contiene la lógica de entrada/salida (la misma del backtest).
- Python decide si cada estrategia/cuenta puede operar y con qué límites.
- Canal: TCP 127.0.0.1, JSON por línea. NT8 es el cliente; Python, el servidor.

### Criterios de aceptación
1. **Base común:** toda estrategia hereda de una clase base `AuthorizedStrategyBase`
   que consulta autorización antes de cada entrada. El código de señales no se
   modifica entre backtest y vivo.
2. **Modo backtest:** en Strategy Analyzer la base no abre socket y autoriza siempre.
3. **Solicitud:** antes de cada entrada envía `{request_id, strategy_id, account,
   instrument, side, qty, stop_distance}`.
4. **Respuesta:** Python devuelve `{request_id, allow, max_qty, reason}`; sin
   respuesta en T ms (parámetro) se deniega.
5. **Fail-closed:** sin conexión o heartbeat (5 s) no hay entradas nuevas. Acción
   sobre posiciones abiertas parametrizable (`hold | flatten`).
6. **Salidas nunca bloqueadas:** SL, TP y cierres de la estrategia se ejecutan sin
   consultar a Python.
7. **Flags dinámicos:** Python puede enviar `PAUSE strategy_id` y `FLATTEN account`;
   NT8 los aplica de inmediato.
8. **Stop obligatorio:** rechaza localmente toda entrada sin stop.
9. **Reglas evaluadas en Python:** pérdida/drawdown restante, consistencia 50%,
   máx. contratos, horario y bloqueo de microscalping (duración mínima de trade).
10. **Telemetría:** NT8 reporta fills, posición y PnL realizado/no realizado por
    estrategia; Python recalcula drawdown y high-water mark.
11. **Reconciliación:** al reiniciar o reconectar, NT8 envía posiciones y órdenes
    reales antes de solicitar autorizaciones.
12. **Auditoría:** cada solicitud, respuesta y evento queda en log con timestamp en hora de Chicago (CST/CDT, configurable en `server.log_timezone`).

### Fuera de alcance
- Exportar resultados de Strategy Analyzer a Python (historia aparte).
- Motor de aprobación y catálogo de reglas por casa.
- Adaptadores Tradeify, Topstep y FTMO.

### Definition of Done
- Compila sin warnings en NT8; una estrategia de ejemplo hereda de la base.
- Mismo código ejecutado en Strategy Analyzer, Sim101 y cuenta de evaluación Lucid.
- Pruebas de falla: Python caído, latencia > T ms, reinicio de NT8 con posición abierta.
- Runbook de instalación en VPS Windows con arranque automático.
