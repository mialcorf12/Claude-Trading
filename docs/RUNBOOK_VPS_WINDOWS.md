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
   git clone <URL_REPOSITORIO> C:\Trading\Claude-Trading
   cd C:\Trading\Claude-Trading
   ```

2. Instalar dependencias requeridas:
   ```cmd
   pip install pyyaml
   ```

3. Verificar que la suite de pruebas pase al 100%:
   ```cmd
   python -m unittest discover tests
   ```
   Deben reportarse todos los tests pasando exitosamente (0 errores, 0 fallos).

---

## 3. Despliegue de Estrategias en NinjaTrader 8

1. Copiar las clases C# a la carpeta de NinjaTrader:
   ```cmd
   copy ninjatrader\Custom\Strategies\AuthorizedStrategyBase.cs "%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Strategies\"
   copy ninjatrader\Custom\Strategies\SampleAuthorizedNQStrategy.cs "%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Strategies\"
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
$Action = New-ScheduledTaskAction -Execute "python.exe" -Argument "C:\Trading\Claude-Trading\src\gate\main.py --config C:\Trading\Claude-Trading\config\lucid_rules.yaml" -WorkingDirectory "C:\Trading\Claude-Trading"
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "LucidTradingGate" -Action $Action -Trigger $Trigger -Principal $Principal
```

### Opción B: Ejecución manual / supervisada (Terminal)
```cmd
cd C:\Trading\Claude-Trading
python -m src.gate.main --config config/lucid_rules.yaml
```

---

## 5. Verificación Operativa

1. **Backtesting en Strategy Analyzer:**
   - Cargar `SampleAuthorizedNQStrategy` en Strategy Analyzer.
   - Correr backtest histórico en NQ o MNQ.
   - Notar que opera normalmente sin requerir socket ni bloquearse.

2. **Operación en Vivo / Sim101:**
   - Aplicar la estrategia a un gráfico de 1 minuto o 5 minutos en NT8.
   - Habilitar `EnableGate = true`.
   - Verificar en la ventana **Output** de NT8:
     ```text
     [YYYY-MM-DD HH:MM:SS.FFF UTC] [ORB_NQ_SAMPLE_01] [CONNECTED] Conectado exitosamente al Gate en 127.0.0.1:8765
     [YYYY-MM-DD HH:MM:SS.FFF UTC] [ORB_NQ_SAMPLE_01] [RECONCILE] Información de posición enviada al Gate
     ```
   - Al generarse una señal, verificar en `logs/gate_audit.log` el registro de auditoría con la autorización concedida o denegada.
