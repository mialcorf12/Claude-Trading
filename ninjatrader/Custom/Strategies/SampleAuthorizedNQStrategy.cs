// ==============================================================================
// NinjaTrader 8 - SampleAuthorizedNQStrategy
// Estrategia de ejemplo basada en Opening Range Breakout (ORB) para NQ / MNQ
// Hereda de AuthorizedStrategyBase.
// ==============================================================================

#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class SampleAuthorizedNQStrategy : AuthorizedStrategyBase
    {
        #region Strategy Parameters
        [NinjaScriptProperty]
        [Range(1, 10)]
        [Display(Name = "Contracts Qty", Description = "Cantidad base de contratos a solicitar", Order = 1, GroupName = "2. Strategy Logic")]
        public int ContractsQty { get; set; } = 2;

        [NinjaScriptProperty]
        [Range(5.0, 100.0)]
        [Display(Name = "Stop Loss (Points)", Description = "Distancia del stop loss en puntos (estricto > 0)", Order = 2, GroupName = "2. Strategy Logic")]
        public double StopLossPoints { get; set; } = 20.0;

        [NinjaScriptProperty]
        [Range(10.0, 300.0)]
        [Display(Name = "Profit Target (Points)", Description = "Distancia del profit target en puntos", Order = 3, GroupName = "2. Strategy Logic")]
        public double ProfitTargetPoints { get; set; } = 40.0;

        [NinjaScriptProperty]
        [Display(Name = "ORB Start Time (CT)", Description = "Inicio del rango de apertura, hora de Chicago (08:30 CT = 09:30 ET)", Order = 4, GroupName = "2. Strategy Logic")]
        public string OrbStartTime { get; set; } = "08:30";

        [NinjaScriptProperty]
        [Display(Name = "ORB End Time (CT)", Description = "Fin del rango de apertura, hora de Chicago (calcula High/Low)", Order = 5, GroupName = "2. Strategy Logic")]
        public string OrbEndTime { get; set; } = "08:45";
        #endregion

        #region Private Variables
        private double orbHigh = double.MinValue;
        private double orbLow = double.MaxValue;
        private bool orbRangeFormed = false;
        private int currentTradingDay = -1;
        private bool tradedToday = false;
        #endregion

        protected override void OnStateChange()
        {
            base.OnStateChange();

            if (State == State.SetDefaults)
            {
                Description = "Estrategia de ejemplo ORB para NQ/MNQ con autorización de riesgo Python";
                Name = "SampleAuthorizedNQStrategy";
                StrategyId = "ORB_NQ_SAMPLE_01";
            }
            else if (State == State.DataLoaded)
            {
                // Configurar salidas gestionadas nativamente por NinjaTrader una vez cargados los datos del instrumento
                int slTicks = TickSize > 0 ? (int)Math.Round(StopLossPoints / TickSize) : (int)(StopLossPoints * 4);
                int tpTicks = TickSize > 0 ? (int)Math.Round(ProfitTargetPoints / TickSize) : (int)(ProfitTargetPoints * 4);
                SetStopLoss(CalculationMode.Ticks, Math.Max(1, slTicks));
                SetProfitTarget(CalculationMode.Ticks, Math.Max(1, tpTicks));
                Print($"[ORB] Salidas configuradas: SL={slTicks} ticks, TP={tpTicks} ticks");
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < BarsRequiredToTrade)
                return;

            // Las ventanas del ORB se expresan en hora de Chicago (reloj del exchange), sin depender de la zona del VPS
            DateTime barTime = ToExchangeTime(Time[0]);

            // Reset diario
            if (barTime.Day != currentTradingDay)
            {
                currentTradingDay = barTime.Day;
                orbHigh = double.MinValue;
                orbLow = double.MaxValue;
                orbRangeFormed = false;
                tradedToday = false;
            }

            TimeSpan t = barTime.TimeOfDay;
            TimeSpan orbStart = TimeSpan.Parse(OrbStartTime);
            TimeSpan orbEnd = TimeSpan.Parse(OrbEndTime);

            // 1. Construir rango de apertura (08:30 a 08:45 CT)
            if (t >= orbStart && t <= orbEnd)
            {
                if (High[0] > orbHigh) orbHigh = High[0];
                if (Low[0] < orbLow) orbLow = Low[0];
                return;
            }

            if (t > orbEnd && !orbRangeFormed && orbHigh > double.MinValue)
            {
                orbRangeFormed = true;
                LogAudit($"[ORB] Rango definido para hoy: High={orbHigh:0.00}, Low={orbLow:0.00}");
            }

            // 2. Disparo de señales tras el rango
            if (orbRangeFormed && !tradedToday && Position.MarketPosition == MarketPosition.Flat)
            {
                // Ruptura alcista del ORB High
                if (Close[0] > orbHigh && Close[1] <= orbHigh)
                {
                    LogAudit($"[SIGNAL] Ruptura alcista detectada en {Close[0]:0.00}. Solicitando autorización...");
                    bool entered = AuthorizedEnterLong("ORB_Long_Entry", ContractsQty, StopLossPoints);
                    if (entered)
                        tradedToday = true;
                }
                // Ruptura bajista del ORB Low
                else if (Close[0] < orbLow && Close[1] >= orbLow)
                {
                    LogAudit($"[SIGNAL] Ruptura bajista detectada en {Close[0]:0.00}. Solicitando autorización...");
                    bool entered = AuthorizedEnterShort("ORB_Short_Entry", ContractsQty, StopLossPoints);
                    if (entered)
                        tradedToday = true;
                }
            }
        }
    }
}
