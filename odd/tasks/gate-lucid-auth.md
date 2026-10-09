# Feature: Gate de Autorización Python ↔ NinjaScript (Lucid Trading)

## Objetivo
Implementar el gate de autorización fail-closed vía TCP entre Python y NinjaTrader 8 según la user story TRD-XX, asegurando que las reglas de riesgo de cuentas LucidFlex (EOD trailing DD, daily loss limit, 50% consistency, max contracts NQ/MNQ, anti-microscalping) se ejecuten estrictamente en tiempo real sin modificar la lógica de señales del backtesting.

## Alcance y Restricciones
- **Entorno:** VPS Windows / Local, NT8 Desktop (.NET Framework 4.8 / C#), Python 3.11+.
- **Protocolo:** TCP 127.0.0.1, JSON por línea (`\n`). NT8 = cliente, Python = servidor.
- **Fail-Closed:** Sin heartbeat (5s) o sin respuesta en T ms -> denegar entradas. Salidas nunca se bloquean.
- **Backtest honesty:** En Strategy Analyzer (NT8) la estrategia opera sin socket y aprueba automáticamente.
- **Reglas Lucid:** Basadas en `Rules/Lucid.xlsx` (25K, 50K, 100K Eval y Funded).
- **Estrategia de entrega:** ask-on-risk (work-unit commits).

## Checklist de Tareas

- [x] **TASK-01: Configuración declarativa de reglas de cuenta**
  - **Ruta:** Inline
  - **Archivos:** `config/lucid_rules.yaml`, `src/gate/config.py`, `tests/test_config.py`
  - **Descripción:** Definir esquemas y valores exactos para 25k, 50k y 100k (Eval y Funded), validando EOD DD, DLL, max contracts NQ/MNQ, 50% consistency, min trade duration, y flattening time (14:55 CT).
  - **Verificación:** `python3 -m unittest tests/test_config.py` (4 tests OK). Commit `0e15913`.

- [x] **TASK-02: Motor de riesgo en Python (`RiskEngine`)**
  - **Ruta:** Inline
  - **Archivos:** `src/gate/risk_engine.py`, `tests/test_risk_engine.py`
  - **Descripción:** Implementar la lógica de negocio de evaluación de órdenes: chequeo de drawdown EOD restante, DLL restante, regla de consistencia 50%, cupo de contratos NQ/MNQ, filtro de horario, bloqueo de micro-scalping (< min duration) y rechazo de órdenes sin stop loss.
  - **Verificación:** `python3 -m unittest tests/test_risk_engine.py` (8 tests OK). Commit `8cbc04e`.

- [x] **TASK-03: Servidor TCP asíncrono y protocolo JSON-lines**
  - **Ruta:** Inline
  - **Archivos:** `src/gate/server.py`, `src/gate/protocol.py`, `tests/test_server.py`
  - **Descripción:** Servidor `asyncio` TCP que escucha en `127.0.0.1:port`. Manejo de solicitudes de autorización (`AUTH_REQUEST`), respuestas (`AUTH_RESPONSE`), latencia máxima parametrizable (timeout T ms), heartbeats cada 5s, telemetría de fills/posiciones/PnL y comandos dinámicos (`PAUSE`, `FLATTEN`).
  - **Verificación:** `python3 -m unittest tests/test_server.py` (2 tests OK). Commit `32ef17f`.

- [x] **TASK-04: Suite de pruebas de integración y simulación de fallos**
  - **Ruta:** Inline
  - **Archivos:** `src/gate/client.py`, `tests/test_integration.py`
  - **Descripción:** Pruebas automatizadas de caída de servidor (fail-closed), timeouts, desconexiones, reconexión con reconciliación de órdenes, y saturación de drawdown.
  - **Verificación:** `python3 -m unittest discover tests` (19 tests OK). Commit `ad7b7b3`.

- [x] **TASK-05: Clase base NinjaScript y estrategia de referencia (`AuthorizedStrategyBase.cs`)**
  - **Ruta:** Inline
  - **Archivos:** `ninjatrader/Custom/Strategies/AuthorizedStrategyBase.cs`, `ninjatrader/Custom/Strategies/SampleAuthorizedNQStrategy.cs`, `src/gate/main.py`, `docs/RUNBOOK_VPS_WINDOWS.md`
  - **Descripción:** Implementar `AuthorizedStrategyBase` en C# para NinjaTrader 8: detección de Strategy Analyzer vs Live/Sim, socket TCP no bloqueante a Python, consulta previa a entries, enforcement de Stop Loss obligatorio local, bypass de SL/TP, listener de flags PAUSE/FLATTEN, telemetría en OnExecutionUpdate y reconciliación al inicio.
  - **Verificación:** Código compilable en .NET Framework 4.8 / NT8, contratos validados contra la suite de tests y runbook de VPS documentado. Commit `7007c2d`.

## Cambios aceptados (2026-10-09)

Origen: aclaraciones del usuario (operador en Costa Rica, UTC-6 fijo; Chicago usa DST). Decisión: reglas de mercado ancladas a `America/Chicago`; zona de logs configurable (`server.log_timezone`, default `America/Chicago`).

- [x] **TASK-06: Zona horaria CST/CT y renombre de campos de sesión**
  - **Ruta:** Inline (acoplado a TASK-07; mismo contexto de código ya mapeado).
  - **Archivos:** `config/lucid_rules.yaml`, `src/gate/config.py`, `src/gate/risk_engine.py`, `src/gate/server.py`, `src/gate/main.py`, `ninjatrader/Custom/Strategies/*.cs`, `tests/*`, `docs/RUNBOOK_VPS_WINDOWS.md`, `userstory.md`
  - **Descripción:** `session_start_time_et`->`session_start_time`, `session_flatten_time_et`->`session_flatten_time` (valores convertidos a hora de Chicago: 08:30 / 14:55). Logs y timestamps en zona configurable en vez de UTC. Corregir break CME a 16:00-17:00 CT (verificado con fuentes públicas; antes estaba 15:00-16:00). Tests sin dependencia de `datetime.now()` (fecha fija).
  - **Verificación:** `python3 -m unittest discover tests` (47 tests OK, RED observado antes de implementar). Timestamps C#/NT8 no verificables en este entorno (sin .NET): requieren F5 en NT8.

- [x] **TASK-07: Cuentas Funded - buffer, payout y balance_for_payout**
  - **Ruta:** Inline.
  - **Archivos:** `config/lucid_rules.yaml`, `src/gate/config.py`, `src/gate/risk_engine.py`, `tests/test_config.py`, `tests/test_risk_engine.py`
  - **Descripción:** Campos `buffer`, `payout` y `balance_for_payout` (= initial_balance + buffer + payout, derivado). `min_account_balance` en funded = piso de liquidación (tope del trailing), no balance de retiro. Bloqueo del día calificado solo después de alcanzar `balance_for_payout` y mientras falten días calificados. `get_payout_status` valida contra `balance_for_payout`.
  - **Datos:** `buffer`/`payout` tomados de `Rules/Lucid.xlsx` (FLEX FUNDED): 25K 1.100/1.000, 50K 2.100/2.000, 100K 3.100/3.000 -> balance_for_payout 27.100 / 54.100 / 106.100. Si faltan, el gate no aplica lógica de payout y avisa al cargar.
  - **Pendiente:** el workbook trae además columnas "FLEX PAYOUT" (fase posterior al primer payout; payout mayor); sin presets todavía.
  - **Verificación:** tests unitarios de ambas fases (acumular balance / calificar días), piso funded y elegibilidad.

## Verificación y Cierre
- Cobertura de pruebas unitarias e integración en Python.
- Verificación de contratos y estados fail-closed.
- Runbook / documentación de despliegue en VPS Windows.
