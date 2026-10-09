// ==============================================================================
// NinjaTrader 8 - AuthorizedStrategyBase
// Gate de autorización Python <-> NinjaScript para cuentas de fondeo (Lucid Trading)
// Compatible con .NET Framework 4.8 / NinjaTrader 8.1+
// ==============================================================================

#region Using declarations
using System;
using System.Collections.Concurrent;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Core.FloatingPoint;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public enum FailClosedAction
    {
        Flatten,
        Hold
    }

    /// <summary>
    /// Clase base para estrategias de NinjaTrader 8 con Gate de autorización de riesgo fail-closed.
    /// En Strategy Analyzer (Backtest) opera sin socket y aprueba automáticamente.
    /// En Live/Sim consulta a Python antes de cada entrada y reporta telemetría.
    /// </summary>
    public abstract class AuthorizedStrategyBase : Strategy
    {
        #region Private Fields
        private TcpClient tcpClient;
        private NetworkStream networkStream;
        private StreamReader reader;
        private StreamWriter writer;
        private Thread receiveThread;
        private volatile bool isRunning;
        private DateTime lastHeartbeatUtc = DateTime.MinValue;
        private readonly object streamLock = new object();
        private readonly ConcurrentDictionary<string, TaskCompletionSource<AuthResponseDto>> pendingRequests =
            new ConcurrentDictionary<string, TaskCompletionSource<AuthResponseDto>>();

        private volatile bool isPausedLocally = false;
        private volatile bool isFlattenedLocally = false;
        #endregion

        #region Properties exposed to NinjaScript UI
        [NinjaScriptProperty]
        [Display(Name = "Strategy Unique ID", Description = "Identificador único de la estrategia para Python", Order = 1, GroupName = "1. Gate Settings")]
        public string StrategyId { get; set; } = "NQ_STRAT_01";

        [NinjaScriptProperty]
        [Display(Name = "Gate Host", Description = "IP del servidor Python", Order = 2, GroupName = "1. Gate Settings")]
        public string GateHost { get; set; } = "127.0.0.1";

        [NinjaScriptProperty]
        [Range(1024, 65535)]
        [Display(Name = "Gate Port", Description = "Puerto TCP del servidor Python", Order = 3, GroupName = "1. Gate Settings")]
        public int GatePort { get; set; } = 8765;

        [NinjaScriptProperty]
        [Range(20, 5000)]
        [Display(Name = "Timeout (ms)", Description = "Tiempo máximo de espera para autorización (T ms)", Order = 4, GroupName = "1. Gate Settings")]
        public int GateTimeoutMs { get; set; } = 200;

        [NinjaScriptProperty]
        [Display(Name = "Fail Action", Description = "Acción al perder socket/heartbeat", Order = 5, GroupName = "1. Gate Settings")]
        public FailClosedAction FailAction { get; set; } = FailClosedAction.Flatten;

        [NinjaScriptProperty]
        [Display(Name = "Enable Gate in Realtime", Description = "Habilita la consulta de autorización en vivo", Order = 6, GroupName = "1. Gate Settings")]
        public bool EnableGate { get; set; } = true;
        #endregion

        #region State Management
        protected override void OnStateChange()
        {
            if (State != State.SetDefaults)
            {
                LogAudit(string.Format("[STATE] Transición a estado: {0}", State));
            }

            if (State == State.SetDefaults)
            {
                Description = "Estrategia con autorización de riesgo externa Python <-> NinjaScript";
                Name = "AuthorizedStrategyBase";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = false; // El aplanado de sesión lo comanda Python mediante el Gate
                ExitOnSessionCloseSeconds = 300;
                IsFillLimitOnTouch = false;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
                OrderFillResolution = OrderFillResolution.Standard;
                StartBehavior = StartBehavior.ImmediatelySubmit;
                TimeInForce = TimeInForce.Gtc;
                TraceOrders = false;
                RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
                StopTargetHandling = StopTargetHandling.PerEntryExecution;
                BarsRequiredToTrade = 20;
            }
            else if (State == State.Historical || State == State.Transition || State == State.Realtime)
            {
                // Conectar en cuanto inicie la estrategia en el gráfico sin esperar al primer tick en vivo
                if (EnableGate && (tcpClient == null || !tcpClient.Connected))
                {
                    ConnectToGate();
                }
            }
            else if (State == State.Terminated)
            {
                DisconnectFromGate();
            }
        }

        /// <summary>
        /// Detecta si estamos en Strategy Analyzer (Backtest / Optimización).
        /// </summary>
        public bool IsBacktestMode()
        {
            // En Strategy Analyzer el State nunca alcanza Transition o Realtime
            return State != State.Realtime && State != State.Transition;
        }
        #endregion

        #region Order Placement & Authorization Gate
        /// <summary>
        /// Solicita autorización al Gate antes de enviar una orden de compra LONG.
        /// Valida stop loss obligatorio (> 0) y ejecuta fail-closed si no hay respuesta.
        /// </summary>
        public bool AuthorizedEnterLong(string signalName, int requestedQty, double stopDistancePoints)
        {
            if (!ValidateStopDistance(stopDistancePoints))
                return false;

            if (IsBacktestMode() || !EnableGate)
            {
                // Modo Backtest: autorización automática inmediata (Criterio 2)
                EnterLong(requestedQty, signalName);
                return true;
            }

            AuthResponseDto auth = RequestGateAuthorization("BUY", requestedQty, stopDistancePoints);
            if (auth.Allow && auth.MaxQty > 0)
            {
                int finalQty = Math.Min(requestedQty, auth.MaxQty);
                LogAudit($"[GATE-OK] Autorizada entrada LONG {finalQty} de {requestedQty} solicitados para {signalName}. Razón: {auth.Reason}");
                EnterLong(finalQty, signalName);
                return true;
            }
            else
            {
                LogAudit($"[GATE-DENIED] Entrada LONG RECHAZADA para {signalName}. Razón: {auth.Reason}");
                return false;
            }
        }

        /// <summary>
        /// Solicita autorización al Gate antes de enviar una orden de venta SHORT.
        /// </summary>
        public bool AuthorizedEnterShort(string signalName, int requestedQty, double stopDistancePoints)
        {
            if (!ValidateStopDistance(stopDistancePoints))
                return false;

            if (IsBacktestMode() || !EnableGate)
            {
                EnterShort(requestedQty, signalName);
                return true;
            }

            AuthResponseDto auth = RequestGateAuthorization("SELL", requestedQty, stopDistancePoints);
            if (auth.Allow && auth.MaxQty > 0)
            {
                int finalQty = Math.Min(requestedQty, auth.MaxQty);
                LogAudit($"[GATE-OK] Autorizada entrada SHORT {finalQty} de {requestedQty} solicitados para {signalName}. Razón: {auth.Reason}");
                EnterShort(finalQty, signalName);
                return true;
            }
            else
            {
                LogAudit($"[GATE-DENIED] Entrada SHORT RECHAZADA para {signalName}. Razón: {auth.Reason}");
                return false;
            }
        }

        private bool ValidateStopDistance(double stopDistancePoints)
        {
            if (stopDistancePoints <= 0)
            {
                LogAudit("[LOCAL-REJECT] ENTRADA RECHAZADA LOCALMENTE: stop_distance debe ser estrictamente > 0 (Criterio 8).");
                return false;
            }
            return true;
        }

        private AuthResponseDto RequestGateAuthorization(string side, int qty, double stopDistancePoints)
        {
            // Chequeo de conexión y latido (Fail-closed)
            if (!IsGateHealthy())
            {
                // Intentar reconectar si la conexión con Python se cayó o se reinició
                ConnectToGate();

                if (!IsGateHealthy())
                {
                    return new AuthResponseDto
                    {
                        Allow = false,
                        MaxQty = 0,
                        Reason = "FAIL_CLOSED: Socket desconectado o heartbeat vencido (> 5s)"
                    };
                }
            }

            if (isPausedLocally || isFlattenedLocally)
            {
                return new AuthResponseDto
                {
                    Allow = false,
                    MaxQty = 0,
                    Reason = "FAIL_CLOSED: Estrategia en PAUSE o cuenta en FLATTEN"
                };
            }

            string requestId = Guid.NewGuid().ToString("N");
            var tcs = new TaskCompletionSource<AuthResponseDto>();
            pendingRequests[requestId] = tcs;

            string jsonReq = string.Format(
                CultureInfo.InvariantCulture,
                "{{\"type\":\"AUTH_REQUEST\",\"request_id\":\"{0}\",\"strategy_id\":\"{1}\",\"account\":\"{2}\",\"instrument\":\"{3}\",\"side\":\"{4}\",\"qty\":{5},\"stop_distance\":{6:0.##}}}\n",
                requestId,
                StrategyId,
                Account != null ? Account.Name : "Sim101",
                Instrument != null ? Instrument.MasterInstrument.Name : "NQ",
                side,
                qty,
                stopDistancePoints
            );

            try
            {
                SendRaw(jsonReq);
                if (tcs.Task.Wait(GateTimeoutMs))
                {
                    return tcs.Task.Result;
                }
                else
                {
                    LogAudit($"[TIMEOUT] Python no respondió en {GateTimeoutMs} ms para solicitud {requestId}.");
                    return new AuthResponseDto
                    {
                        Allow = false,
                        MaxQty = 0,
                        Reason = $"TIMEOUT_EXCEEDED: Gate superó los {GateTimeoutMs} ms"
                    };
                }
            }
            catch (Exception ex)
            {
                LogAudit($"[SOCKET-ERROR] Excepción consultando al Gate: {ex.Message}");
                return new AuthResponseDto
                {
                    Allow = false,
                    MaxQty = 0,
                    Reason = $"EXCEPTION: {ex.Message}"
                };
            }
            finally
            {
                TaskCompletionSource<AuthResponseDto> dummy;
                pendingRequests.TryRemove(requestId, out dummy);
            }
        }
        #endregion

        #region Telemetry & Reconciliation
        protected override void OnExecutionUpdate(Execution execution, string executionId, double price, int quantity, MarketPosition marketPosition, string orderId, DateTime time)
        {
            base.OnExecutionUpdate(execution, executionId, price, quantity, marketPosition, orderId, time);

            if (IsBacktestMode() || !EnableGate || !IsGateHealthy())
                return;

            // Reportar telemetría a Python (Criterio 10)
            SendTelemetry();
        }

        /// <summary>
        /// Envía balance y PnL no realizado de la cuenta. El PnL del día lo calcula Python
        /// (balance actual - balance al inicio del día de trading), por lo que no se envía realized_pnl_today.
        /// </summary>
        private void SendTelemetry()
        {
            try
            {
                double balance = Account != null ? Account.Get(AccountItem.CashValue, Currency.UsDollar) : 0.0;
                double unrealized = Account != null ? Account.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar) : 0.0;

                string jsonTele = string.Format(
                    CultureInfo.InvariantCulture,
                    "{{\"type\":\"TELEMETRY\",\"account\":\"{0}\",\"strategy_id\":\"{1}\",\"current_balance\":{2:0.##},\"unrealized_pnl\":{3:0.##}}}\n",
                    Account != null ? Account.Name : "Sim101",
                    StrategyId,
                    balance,
                    unrealized
                );
                SendRaw(jsonTele);
            }
            catch (Exception ex)
            {
                LogAudit($"[TELEMETRY-ERR] Error enviando telemetría: {ex.Message}");
            }
        }

        private void SendReconciliation()
        {
            try
            {
                int currentPosQty = Position != null ? Math.Abs(Position.Quantity) : 0;
                string side = Position != null && Position.MarketPosition == MarketPosition.Long ? "LONG" :
                              Position != null && Position.MarketPosition == MarketPosition.Short ? "SHORT" : "FLAT";
                double entryPrice = Position != null ? Position.AveragePrice : 0.0;

                string posJsonArray = currentPosQty > 0
                    ? string.Format(CultureInfo.InvariantCulture, "[{{\"instrument\":\"{0}\",\"qty\":{1},\"entry_price\":{2:0.##},\"side\":\"{3}\"}}]",
                        Instrument != null ? Instrument.MasterInstrument.Name : "NQ", currentPosQty, entryPrice, side)
                    : "[]";

                string jsonRec = string.Format(
                    "{{\"type\":\"RECONCILE\",\"account\":\"{0}\",\"positions\":{1}}}\n",
                    Account != null ? Account.Name : "Sim101",
                    posJsonArray
                );
                SendRaw(jsonRec);
                LogAudit("[RECONCILE] Información de posición enviada al Gate");
            }
            catch (Exception ex)
            {
                LogAudit($"[RECONCILE-ERR] Error enviando reconciliación: {ex.Message}");
            }
        }
        #endregion

        #region TCP Socket & Background Thread
        private void ConnectToGate()
        {
            try
            {
                DisconnectFromGate();

                tcpClient = new TcpClient();
                tcpClient.Connect(GateHost, GatePort);
                networkStream = tcpClient.GetStream();
                reader = new StreamReader(networkStream, Encoding.UTF8);
                writer = new StreamWriter(networkStream, new UTF8Encoding(false)) { AutoFlush = true };

                isRunning = true;
                lastHeartbeatUtc = DateTime.UtcNow;

                receiveThread = new Thread(ReceiveLoop) { IsBackground = true, Name = "GateClient_" + StrategyId };
                receiveThread.Start();

                LogAudit($"[CONNECTED] Conectado exitosamente al Gate en {GateHost}:{GatePort}");

                // Enviar reconciliación inicial de posición (Criterio 11)
                SendReconciliation();
                // Balance inicial: permite a Python fijar el baseline del día y calcular el PnL diario
                SendTelemetry();
            }
            catch (Exception ex)
            {
                LogAudit($"[CONN-FAIL] No se pudo conectar al Gate ({ex.Message}). Estrategia en modo fail-closed.");
            }
        }

        private void DisconnectFromGate()
        {
            isRunning = false;
            try
            {
                if (writer != null) writer.Close();
                if (reader != null) reader.Close();
                if (networkStream != null) networkStream.Close();
                if (tcpClient != null) tcpClient.Close();
            }
            catch { }
        }

        private bool IsGateHealthy()
        {
            if (!isRunning || tcpClient == null || !tcpClient.Connected)
                return false;

            // Heartbeat de 5 segundos con margen de 5 segundos adicionales (Criterio 5)
            if ((DateTime.UtcNow - lastHeartbeatUtc).TotalSeconds > 10.0)
            {
                HandleHeartbeatFailure();
                return false;
            }
            return true;
        }

        private void HandleHeartbeatFailure()
        {
            LogAudit("[HEARTBEAT-LOST] Heartbeat perdido con Python por más de 10s. Aplicando FailAction...");
            if (FailAction == FailClosedAction.Flatten && Position != null && Position.MarketPosition != MarketPosition.Flat)
            {
                LogAudit("[FAIL-FLATTEN] Aplanando posición inmediatamente por pérdida de heartbeat.");
                if (Position.MarketPosition == MarketPosition.Long)
                    ExitLong();
                else if (Position.MarketPosition == MarketPosition.Short)
                    ExitShort();
            }
        }

        private void SendRaw(string line)
        {
            lock (streamLock)
            {
                if (writer != null && tcpClient != null && tcpClient.Connected)
                {
                    writer.Write(line);
                }
            }
        }

        private void ReceiveLoop()
        {
            while (isRunning && reader != null)
            {
                try
                {
                    string line = reader.ReadLine();
                    if (line == null) break;

                    ParseAndDispatch(line);
                }
                catch (Exception)
                {
                    break;
                }
            }
            isRunning = false;
            LogAudit("[DISCONNECTED] Desconectado del Gate.");
        }

        private void ParseAndDispatch(string line)
        {
            line = line.Trim();
            if (string.IsNullOrEmpty(line)) return;

            // Manejo de Heartbeat
            if (line.Contains("\"type\":\"HEARTBEAT\"") || line.Contains("\"type\": \"HEARTBEAT\""))
            {
                lastHeartbeatUtc = DateTime.UtcNow;
                SendRaw("{\"type\":\"HEARTBEAT_ACK\"}\n");
                return;
            }

            // Manejo de Auth Response
            if (line.Contains("\"type\":\"AUTH_RESPONSE\"") || line.Contains("\"type\": \"AUTH_RESPONSE\""))
            {
                string reqId = ExtractJsonString(line, "request_id");
                bool allow = line.Contains("\"allow\":true") || line.Contains("\"allow\": true");
                int maxQty = ExtractJsonInt(line, "max_qty");
                string reason = ExtractJsonString(line, "reason");

                TaskCompletionSource<AuthResponseDto> tcs;
                if (!string.IsNullOrEmpty(reqId) && pendingRequests.TryGetValue(reqId, out tcs))
                {
                    tcs.TrySetResult(new AuthResponseDto
                    {
                        RequestId = reqId,
                        Allow = allow,
                        MaxQty = maxQty,
                        Reason = reason
                    });
                }
                return;
            }

            // Manejo de Comandos Dinámicos (PAUSE / FLATTEN) (Criterio 7)
            if (line.Contains("\"type\":\"COMMAND\"") || line.Contains("\"type\": \"COMMAND\""))
            {
                string action = ExtractJsonString(line, "action");
                if (action == "PAUSE")
                {
                    string targetStrat = ExtractJsonString(line, "strategy_id");
                    if (string.IsNullOrEmpty(targetStrat) || targetStrat == StrategyId)
                    {
                        isPausedLocally = true;
                        LogAudit($"[COMMAND] Estrategia {StrategyId} PAUSADA por comando de Python.");
                    }
                }
                else if (action == "FLATTEN")
                {
                    isFlattenedLocally = true;
                    LogAudit($"[COMMAND] FLATTEN recibido de Python para cuenta {Account?.Name}. Aplanando posiciones...");
                    if (Position != null && Position.MarketPosition == MarketPosition.Long)
                        ExitLong();
                    else if (Position != null && Position.MarketPosition == MarketPosition.Short)
                        ExitShort();
                }
            }
        }

        #region Minimal JSON String Parsing Helpers
        private string ExtractJsonString(string json, string key)
        {
            string pattern = "\"" + key + "\":\"";
            int idx = json.IndexOf(pattern);
            if (idx == -1)
            {
                pattern = "\"" + key + "\": \"";
                idx = json.IndexOf(pattern);
            }
            if (idx == -1) return string.Empty;

            int start = idx + pattern.Length;
            int end = json.IndexOf("\"", start);
            return end > start ? json.Substring(start, end - start) : string.Empty;
        }

        private int ExtractJsonInt(string json, string key)
        {
            string pattern = "\"" + key + "\":";
            int idx = json.IndexOf(pattern);
            if (idx == -1) return 0;

            int start = idx + pattern.Length;
            while (start < json.Length && (json[start] == ' ' || json[start] == ':')) start++;
            int end = start;
            while (end < json.Length && (char.IsDigit(json[end]) || json[end] == '-')) end++;
            int val;
            return int.TryParse(json.Substring(start, end - start), out val) ? val : 0;
        }
        #endregion

        #endregion

        #region Helpers & Audit
        // Hora oficial del CME = Chicago. El ID de Windows "Central Standard Time" es US Central (sigue el DST
        // de Chicago: CST invierno / CDT verano). No confundir con "Central America Standard Time" (Costa Rica, UTC-6 fijo).
        private static readonly TimeZoneInfo ExchangeTimeZone = ResolveExchangeTimeZone();

        private static TimeZoneInfo ResolveExchangeTimeZone()
        {
            try
            {
                return TimeZoneInfo.FindSystemTimeZoneById("Central Standard Time");
            }
            catch (Exception)
            {
                return TimeZoneInfo.Local;
            }
        }

        /// <summary>
        /// Convierte un instante de barra de NinjaTrader (hora local de la máquina) a hora de Chicago.
        /// </summary>
        protected static DateTime ToExchangeTime(DateTime localTime)
        {
            return TimeZoneInfo.ConvertTime(localTime, TimeZoneInfo.Local, ExchangeTimeZone);
        }

        private static string FormatLogTimestamp(DateTime utcNow)
        {
            DateTime chicago = TimeZoneInfo.ConvertTimeFromUtc(utcNow, ExchangeTimeZone);
            string abbreviation = ExchangeTimeZone.IsDaylightSavingTime(chicago) ? "CDT" : "CST";
            return string.Format("{0:yyyy-MM-dd HH:mm:ss.fff} {1}", chicago, abbreviation);
        }

        protected void LogAudit(string message)
        {
            Print(string.Format("[{0}] [{1}] {2}", FormatLogTimestamp(DateTime.UtcNow), StrategyId, message));
        }
        #endregion
    }

    public class AuthResponseDto
    {
        public string RequestId { get; set; }
        public bool Allow { get; set; }
        public int MaxQty { get; set; }
        public string Reason { get; set; }
    }
}
