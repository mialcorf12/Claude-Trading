# Runbook de Instalación en VPS Windows - Gate Lucid Trading

Este runbook describe el procedimiento para desplegar el servidor Python del Gate de autorización y las estrategias NinjaScript en un entorno VPS Windows con NinjaTrader 8.

---

## 1. Requisitos Previos

Ejecutar en PowerShell (Administrador) en el VPS:
```powershell
python validate_environment.py
```
Asegurarse de que todos los checks críticos den `[OK]`:
- Python 3.11+
- .NET Framework 4.8
- NinjaTrader 8 instalado
- Carpeta de Strategies disponible

---

## 2. Instalación del Servidor Python

1. Clonar el repositorio en el VPS:
   ```cmd
   git clone https://github.com/mialcorf12/Claude-Trading.git C:\TradingPlatform\Claude-Trading
   cd C:\TradingPlatform\Claude-Trading
   ```

2. Instalar dependencias requeridas:
   ```cmd
   pip install pyyaml tzdata
   ```

3. Verificar que la suite de pruebas pase al 100%:
   ```cmd
   python -m unittest discover tests
   ```
   Deben reportarse todos los tests pasando exitosamente (0 errores, 0 fallos).

Es correcto recibir algo como esto:
....Timeout esperando autorizacion para req-slow en 50 ms
Executing <Task pending name='Task-2' coro=<TestIntegrationAndFailures.test_failure_latency_timeout_denies_entry() running at C:\TradingPlatform\Claude-Trading\tests\test_integration.py:105> wait_for=<Task cancelling name='Task-8' coro=<GateClient._read_loop() running at C:\TradingPlatform\Claude-Trading\src\gate\client.py:196> wait_for=<Future cancelled created at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\base_events.py:460> cb=[Task.task_wakeup()] created at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\tasks.py:395> cb=[_run_until_complete_cb() at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\base_events.py:181] created at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\runners.py:110> took 0.450 seconds
..Fallo al conectar con el Gate en 127.0.0.1:9888: [WinError 1225] The remote computer refused the network connection
Executing <Task finished name='Task-24' coro=<TestIntegrationAndFailures.test_failure_python_server_down_fail_closed() done, defined at C:\TradingPlatform\Claude-Trading\tests\test_integration.py:52> result=None created at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\runners.py:110> took 0.274 seconds
...Executing <Handle BaseProactorEventLoop._loop_self_reading() created at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\windows_events.py:320> took 0.222 seconds
........Cuenta Sim101 marcada para FLATTEN obligatorio
Executing <Task pending name='Task-48' coro=<TestGateServer.test_dynamic_commands_broadcast() running at C:\TradingPlatform\Claude-Trading\tests\test_server.py:121> wait_for=<Future pending cb=[Task.task_wakeup()] created at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\base_events.py:460> cb=[_run_until_complete_cb() at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\base_events.py:181] created at C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\asyncio\runners.py:110> took 0.324 seconds
..
----------------------------------------------------------------------
Ran 19 tests in 10.854s
---

## 3. Despliegue de Estrategias en NinjaTrader 8

1. Copiar las clases C# a la carpeta de NinjaTrader: (Ninjatrader cerrado)
   ```cmd
   copy ninjatrader\Custom\Strategies\AuthorizedStrategyBase.cs "C:\Users\Administrator\Documents\NinjaTrader 8\bin\Custom\Strategies\"
   copy ninjatrader\Custom\Strategies\SampleAuthorizedNQStrategy.cs "C:\Users\Administrator\Documents\NinjaTrader 8\bin\Custom\Strategies\"
   ```

2. En NinjaTrader 8:
   - Abrir NinjaTrader 8 Desktop.
   - Ir a **Tools** > **NinjaScript Editor** (F5 para compilar).
   - Verificar que compile con cero errores.

---

## 4. Configuración del Arranque Automático en Windows

Para que el servidor Python arranque automáticamente al iniciar el VPS sin depender de una sesión de usuario abierta:

### Opción A: Mediante Tarea Programada (Task Scheduler)
Crear una tarea programada para ejecutarse al iniciar el sistema con privilegios elevados:
```powershell
$Action = New-ScheduledTaskAction -Execute "python.exe" -Argument "C:\TradingPlatform\Claude-Trading\src\gate\main.py --config C:\TradingPlatform\Claude-Trading\config\lucid_rules.yaml" -WorkingDirectory "C:\TradingPlatform\Claude-Trading"
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "LucidTradingGate" -Action $Action -Trigger $Trigger -Principal $Principal
```

### Opción B: Ejecución manual / supervisada (Terminal)
```cmd
cd C:\TradingPlatform\Claude-Trading
python -m src.gate.main --config config/lucid_rules.yaml
```

---

## 5. Verificación Operativa

1. **Backtesting en Strategy Analyzer:**
   - Cargar `SampleAuthorizedNQStrategy` en Strategy Analyzer.
   - Correr backtest histórico en NQ o MNQ.
   - Notar que opera normalmente sin requerir socket ni bloquearse.

2. **Operación en Vivo / Sim101:**
   - Activar la estrategia en NinjaTrader 8
   - Abrí un chart de MNQ (1 minuto) asignado a la cuenta Sim101.
   - Hacé click derecho en el chart > Strategies > seleccioná SampleAuthorizedNQStrategy.
   - Verificá que en el panel derecho:
      - Enable Gate in Realtime = True
      - Gate Host = 127.0.0.1
      - Gate Port = 8765
      - Enabled = True
      - Dale a OK.
   - Revisá la ventana Output de NinjaTrader (New > NinjaScript Output). Deberías ver:
     ```text
     [YYYY-MM-DD HH:MM:SS.FFF CDT] [ORB_NQ_SAMPLE_01] [CONNECTED] Conectado exitosamente al Gate en 127.0.0.1:8765
     [YYYY-MM-DD HH:MM:SS.FFF CDT] [ORB_NQ_SAMPLE_01] [RECONCILE] Información de posición enviada al Gate
     ```
   - Al generarse una señal, verificar en `logs/gate_audit.log` el registro de auditoría con la autorización concedida o denegada.

Activar la estrategia en NinjaTrader 8
Abrí un chart de MNQ (1 minuto) asignado a la cuenta Sim101.
Hacé click derecho en el chart > Strategies > seleccioná SampleAuthorizedNQStrategy.
Verificá que en el panel derecho:
Enable Gate in Realtime = True
Gate Host = 127.0.0.1
Gate Port = 8765
Enabled = True
Dale a OK.
Revisá la ventana Output de NinjaTrader (New > NinjaScript Output). Deberías ver:
[CONNECTED] Conectado exitosamente al Gate en 127.0.0.1:8765
[RECONCILE] Información de posición enviada al Gate
Y en la terminal de Python verás el log del cliente conectado y la reconciliación procesada.

---

## 6. Zonas horarias (importante)

- **Reglas de mercado = hora de Chicago (`America/Chicago`)**: break diario del CME 16:00-17:00 CT (17:00-18:00 ET), cierre semanal el viernes 16:00 CT y reapertura el domingo 17:00 CT. Siguen el horario de verano de Chicago automaticamente.
- **Costa Rica es UTC-6 todo el año**; Chicago es UTC-5 (CDT) de marzo a noviembre. En verano el break se ve a las **15:00-16:00 en el reloj de Costa Rica**; en invierno coincide con Chicago (16:00-17:00).
- **Logs y timestamps** usan `server.log_timezone` (default `America/Chicago`, con sufijo CST/CDT). Para ver el reloj fijo de Costa Rica: `log_timezone: "America/Costa_Rica"`.
- `tzdata` es obligatorio en Windows para que Python resuelva `America/Chicago`.
- Los campos `session_start_time` / `session_flatten_time` de cada preset estan en hora de Chicago y solo aplican en `session.mode: "rth_only"`.

---

## 7. Dia de trading, estado persistido y `record_closed_day`

- **Dia de trading = 16:00 CT a 16:00 CT** (17:00 ET, liquidacion del CME), configurable en `session.trading_day_rollover`. En reloj de Costa Rica: 15:00 en verano, 16:00 en invierno.
- Al cruzar ese corte el gate **cierra el dia automaticamente** (tick cada 30 s, y tambien al recibir telemetria o autorizar):
  - registra el PnL del dia (`record_closed_day`): en Funded/Payout cuenta como dia calificado si supera `min_profit_day_amount`;
  - sube el **HWM del trailing EOD** con el balance de cierre (el HWM ya no sube intradia);
  - reinicia PnL diario y el flatten diario.
- **PnL del dia = balance actual - balance al inicio del dia.** NT8 envia `CashValue` como balance; el gate calcula el resto.
- **Estado persistido** en `state/gate_state.json` (`server.state_path`): baseline diario, HWM EOD, dias calificados y ultimos 90 dias cerrados. Sobrevive reinicios del gate. Si el archivo es ilegible se ignora y se registra un warning.
- **Primer arranque de una cuenta:** el baseline es el primer balance que reporte NT8. Arranca el gate **antes del corte de las 16:00 CT** (o al inicio de la sesion) para que el PnL del primer dia sea correcto; si lo arrancas a mitad del dia, el PnL ya realizado ese dia no cuenta.
- **No borres `state/`** en una cuenta Funded/Payout: contiene el historial de dias calificados para el payout. Respaldalo junto con `logs/`.

---

## 8. Dashboard web (solo lectura)

Muestra un resumen macro del gate y permite filtrar por **cuenta** y por **dia de trading** (16:00 a 16:00 CT). Lee `state/gate_state.json` y `logs/gate_audit.log`; no escribe nada ni toca el trading, y corre como proceso aparte (sin dependencias nuevas).

```powershell
cd C:\TradingPlatform\Claude-Trading
python -m src.dashboard.main --config config/lucid_rules.yaml
```

Abre **http://127.0.0.1:8780** en el navegador del VPS (por RDP). Opciones: `--port 8780`, `--host 127.0.0.1`.

- **Se actualiza solo cada 5 min** (casilla "Auto-actualizar") o con el boton "Actualizar ahora". El filtro queda en la URL, asi que puedes guardar el enlace.
- **Resumen:** estado del gate, decisiones aprobadas/rechazadas, tasa de aprobacion, latencia de decision, motivos de rechazo.
- **Cuentas (snapshot actual):** colchon de drawdown, DLL restante, progreso al profit target (Eval) o al payout y dias calificados (Funded/Payout). Estados: En orden / Atencion (colchon < 30% del max loss o DLL usado >= 70%) / Critico.
- **Dias cerrados, decisiones y eventos** con filtros por cuenta y dia.
- **Limite:** solo ve lo que Python registro. Los rechazos locales de NT8 (timeout, socket caido) quedan en la ventana Output de NinjaTrader.
- **Seguridad:** no tiene autenticacion. Por defecto escucha solo en `127.0.0.1`; si usas `--host 0.0.0.0`, restringelo por firewall/VPN.
- **Arranque automatico (opcional):** crea una segunda tarea programada igual a la del gate (seccion 4) con el argumento `-m src.dashboard.main --config config/lucid_rules.yaml`.
