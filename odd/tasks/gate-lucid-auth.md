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

- [ ] **TASK-01: Configuración declarativa de reglas de cuenta**
  - **Ruta:** Inline
  - **Archivos:** `config/lucid_rules.yaml`, `src/gate/config.py`
  - **Descripción:** Definir esquemas y valores exactos para 25k, 50k y 100k (Eval y Funded), validando EOD DD, DLL, max contracts NQ/MNQ, 50% consistency, min trade duration, y flattening time (15:55 ET).
  - **Verificación:** Pruebas de carga y validación de esquemas de configuración.

- [ ] **TASK-02: Motor de riesgo en Python (`RiskEngine`)**
  - **Ruta:** Inline
  - **Archivos:** `src/gate/risk_engine.py`
  - **Descripción:** Implementar la lógica de negocio de evaluación de órdenes: chequeo de drawdown EOD restante, DLL restante, regla de consistencia 50%, cupo de contratos NQ/MNQ, filtro de horario, bloqueo de micro-scalping (< min duration) y rechazo de órdenes sin stop loss.
  - **Verificación:** Pruebas unitarias de cada regla y casos combinados.

- [ ] **TASK-03: Servidor TCP asíncrono y protocolo JSON-lines**
  - **Ruta:** Inline
  - **Archivos:** `src/gate/server.py`, `src/gate/protocol.py`
  - **Descripción:** Servidor `asyncio` TCP que escucha en `127.0.0.1:port`. Manejo de solicitudes de autorización (`AUTH_REQUEST`), respuestas (`AUTH_RESPONSE`), latencia máxima parametrizable (timeout T ms), heartbeats cada 5s, telemetría de fills/posiciones/PnL y comandos dinámicos (`PAUSE`, `FLATTEN`).
  - **Verificación:** Tests de integración cliente-servidor con sockets asíncronos.

- [ ] **TASK-04: Suite de pruebas de integración y simulación de fallos**
  - **Ruta:** Inline
  - **Archivos:** `tests/test_config.py`, `tests/test_risk_engine.py`, `tests/test_server.py`, `tests/test_integration.py`
  - **Descripción:** Pruebas automatizadas de caída de servidor (fail-closed), timeouts, desconexiones, reconexión con reconciliación de órdenes, y saturación de drawdown.
  - **Verificación:** Ejecución de suite de tests en Python (`pytest` o `unittest`).

- [ ] **TASK-05: Clase base NinjaScript y estrategia de referencia (`AuthorizedStrategyBase.cs`)**
  - **Ruta:** Inline
  - **Archivos:** `ninjatrader/Custom/Strategies/AuthorizedStrategyBase.cs`, `ninjatrader/Custom/Strategies/SampleAuthorizedNQStrategy.cs`
  - **Descripción:** Implementar `AuthorizedStrategyBase` en C# para NinjaTrader 8: detección de Strategy Analyzer vs Live/Sim, socket TCP no bloqueante a Python, consulta previa a entries, enforcement de Stop Loss obligatorio local, bypass de SL/TP, listener de flags PAUSE/FLATTEN, telemetría en OnExecutionUpdate y reconciliación al inicio.
  - **Verificación:** Validación estática de código C#, compatibilidad con NT8 assemblies y revisión de contratos de API.

## Verificación y Cierre
- Cobertura de pruebas unitarias e integración en Python.
- Verificación de contratos y estados fail-closed.
- Runbook / documentación de despliegue en VPS Windows.
