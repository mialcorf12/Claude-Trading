"""Tests de la capa de datos del dashboard (lee state/gate_state.json y logs/gate_audit.log)."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
import zoneinfo

import socket

from src.dashboard.data import DashboardSources, build_overview, probe_port, read_audit
from src.gate.config import load_config

CT = zoneinfo.ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=CT)  # jueves -> dia de trading 2026-10-08


def stamp(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=CT).isoformat()


def rec(ts, event, **data):
    return {"timestamp": ts, "event": event, "data": data}


def request_pair(day, hour, minute, req_id, account, allow, reason, latency_s=0.02, strategy="ORB_01"):
    t0 = datetime(2026, 10, day, hour, minute, 0, tzinfo=CT)
    t1 = t0.replace(microsecond=int(latency_s * 1_000_000))
    return [
        rec(t0.isoformat(), "AUTH_REQUEST_RECEIVED", type="AUTH_REQUEST", request_id=req_id, strategy_id=strategy,
            account=account, instrument="MNQ", side="BUY", qty=2, stop_distance=20.0),
        rec(t1.isoformat(), "AUTH_RESPONSE_SENT", type="AUTH_RESPONSE", request_id=req_id, allow=allow,
            max_qty=2 if allow else 0, reason=reason),
    ]


class DashboardDataBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = load_config("config/lucid_rules.yaml")
        self.config.accounts["Funded_25K_01"] = "25k_flex_funded"
        self.sources = DashboardSources(
            state_path=self.dir / "gate_state.json", audit_path=self.dir / "gate_audit.log", config=self.config
        )

    def tearDown(self):
        self.tmp.cleanup()

    def write_audit(self, records):
        self.sources.audit_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    def write_state(self, accounts):
        self.sources.state_path.write_text(json.dumps({"version": 1, "accounts": accounts}), encoding="utf-8")

    def overview(self, **kwargs):
        return build_overview(self.sources, now=NOW, **kwargs)


class TestReadAudit(DashboardDataBase):
    def test_parses_records_and_skips_malformed_lines(self):
        good = rec(stamp(8, 9), "SERVER_START", host="127.0.0.1", port=8765)
        self.sources.audit_path.write_text(json.dumps(good) + "\nesto no es json\n\n", encoding="utf-8")
        records, skipped = read_audit(self.sources.audit_path)
        self.assertEqual([r["event"] for r in records], ["SERVER_START"])
        self.assertEqual(skipped, 1)

    def test_accepts_legacy_timestamp_utc_key(self):
        legacy = {"timestamp_utc": "2026-10-08T17:00:00+00:00", "event": "SERVER_START", "data": {}}
        self.sources.audit_path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        records, _ = read_audit(self.sources.audit_path)
        self.assertEqual(len(records), 1)

    def test_missing_file_returns_empty(self):
        self.assertEqual(read_audit(self.dir / "nope.log"), ([], 0))

    def test_only_reads_the_tail_of_a_huge_file(self):
        lines = [json.dumps(rec(stamp(8, 9, i % 60), "CLIENT_CONNECTED", peer=str(i))) for i in range(200)]
        self.sources.audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        records, _ = read_audit(self.sources.audit_path, max_bytes=2000)
        self.assertLess(len(records), 200)
        self.assertEqual(records[-1]["data"]["peer"], "199")  # conserva lo mas reciente, sin lineas partidas


class TestDecisionsAndSummary(DashboardDataBase):
    def setUp(self):
        super().setUp()
        records = []
        records += request_pair(8, 9, 30, "a1", "Sim101", True, "APPROVED", latency_s=0.010)
        records += request_pair(8, 9, 45, "a2", "Sim101", False, "MAX_CONTRACTS_EXCEEDED: Solicitados 30", latency_s=0.030)
        records += request_pair(8, 10, 0, "a3", "Lucid_50K_01", False, "DAILY_LOSS_LIMIT_REACHED: x", latency_s=0.020)
        records += request_pair(7, 11, 0, "b1", "Sim101", True, "APPROVED", latency_s=0.015)  # dia anterior
        records.append(rec(stamp(8, 11, 0), "AUTH_REQUEST_RECEIVED", type="AUTH_REQUEST", request_id="c1",
                           strategy_id="ORB_01", account="Sim101", instrument="MNQ", side="BUY", qty=1, stop_distance=5.0))
        self.write_audit(records)

    def test_decisions_join_request_and_response_with_latency(self):
        decisions = {d["request_id"]: d for d in self.overview()["decisions"]}
        self.assertTrue(decisions["a1"]["allow"])
        self.assertEqual(decisions["a1"]["latency_ms"], 10.0)
        self.assertEqual(decisions["a2"]["reason_code"], "MAX_CONTRACTS_EXCEEDED")
        self.assertIsNone(decisions["c1"]["allow"])  # sin respuesta
        self.assertEqual(decisions["a2"]["account"], "Sim101")

    def test_summary_counts_and_rates(self):
        summary = self.overview()["summary"]["decisions"]
        self.assertEqual((summary["total"], summary["allowed"], summary["denied"], summary["no_response"]), (5, 2, 2, 1))
        self.assertEqual(summary["approval_rate"], 0.5)  # 2 aprobadas / 4 respondidas
        self.assertEqual(summary["latency_ms_max"], 30.0)
        self.assertEqual({r["code"] for r in summary["top_reject_reasons"]},
                         {"MAX_CONTRACTS_EXCEEDED", "DAILY_LOSS_LIMIT_REACHED"})

    def test_filter_by_account(self):
        overview = self.overview(account="Sim101")
        self.assertEqual({d["account"] for d in overview["decisions"]}, {"Sim101"})
        self.assertEqual(overview["summary"]["decisions"]["total"], 4)

    def test_filter_by_trading_day(self):
        overview = self.overview(day="2026-10-07")
        self.assertEqual([d["request_id"] for d in overview["decisions"]], ["b1"])

    def test_filter_by_account_and_day(self):
        overview = self.overview(account="Sim101", day="2026-10-08")
        self.assertEqual(sorted(d["request_id"] for d in overview["decisions"]), ["a1", "a2", "c1"])

    def test_trading_day_cuts_at_16_00_chicago(self):
        records = request_pair(8, 15, 59, "before", "Sim101", True, "APPROVED")
        records += request_pair(8, 16, 1, "after", "Sim101", True, "APPROVED")
        self.write_audit(records)
        by_day = {d["request_id"]: d["trading_day"] for d in self.overview()["decisions"]}
        self.assertEqual(by_day, {"before": "2026-10-08", "after": "2026-10-09"})

    def test_meta_lists_accounts_and_days_for_the_filters(self):
        meta = self.overview()["meta"]
        self.assertIn("Sim101", meta["accounts"])
        self.assertIn("Lucid_50K_01", meta["accounts"])
        self.assertEqual(meta["days"][:2], ["2026-10-08", "2026-10-07"])  # mas reciente primero


class TestGateStatusAndEvents(DashboardDataBase):
    def test_running_gate_counts_connected_clients_since_last_start(self):
        self.write_audit([
            rec(stamp(8, 8), "SERVER_START", host="127.0.0.1", port=8765),
            rec(stamp(8, 8, 1), "CLIENT_CONNECTED", peer="a"),
            rec(stamp(8, 8, 2), "CLIENT_CONNECTED", peer="b"),
            rec(stamp(8, 8, 3), "CLIENT_DISCONNECTED", peer="a"),
        ])
        gate = self.overview()["gate"]
        self.assertEqual(gate["status"], "running")
        self.assertEqual(gate["connected_clients"], 1)
        self.assertEqual(gate["last_event_age_seconds"], (NOW - datetime.fromisoformat(stamp(8, 8, 3))).total_seconds())

    def test_stopped_gate(self):
        self.write_audit([rec(stamp(8, 8), "SERVER_START"), rec(stamp(8, 9), "SERVER_STOP")])
        self.assertEqual(self.overview()["gate"]["status"], "stopped")

    def test_no_log_yet(self):
        self.assertEqual(self.overview()["gate"]["status"], "no_data")

    def test_events_are_filtered_and_summarised(self):
        self.write_audit([
            rec(stamp(8, 9), "TELEMETRY_RECEIVED", type="TELEMETRY", account="Sim101", strategy_id="s",
                current_balance=25150.0, unrealized_pnl=0),
            rec(stamp(8, 9, 5), "RECONCILE_RECEIVED", type="RECONCILE", account="Lucid_50K_01", positions=[]),
            rec(stamp(8, 9, 6), "COMMAND_BROADCAST", type="COMMAND", action="FLATTEN", account="Sim101"),
        ])
        events = self.overview(account="Sim101")["events"]
        self.assertEqual({e["event"] for e in events}, {"TELEMETRY_RECEIVED", "COMMAND_BROADCAST"})
        telemetry = next(e for e in events if e["event"] == "TELEMETRY_RECEIVED")
        self.assertIn("25150", telemetry["detail"])


class TestGateStatusFromPort(DashboardDataBase):
    """El estado en linea se decide por el puerto del gate, no solo por el log (un gate caido no escribe SERVER_STOP)."""

    def with_address(self, port):
        self.sources = DashboardSources(
            state_path=self.sources.state_path, audit_path=self.sources.audit_path,
            config=self.config, gate_address=("127.0.0.1", port),
        )

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_probe_port(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            self.assertTrue(probe_port("127.0.0.1", server.getsockname()[1]))
        self.assertFalse(probe_port("127.0.0.1", self.free_port()))

    def test_crashed_gate_is_reported_stopped_even_if_the_log_says_running(self):
        self.write_audit([rec(stamp(8, 8), "SERVER_START"), rec(stamp(8, 8, 5), "CLIENT_CONNECTED", peer="a")])
        self.with_address(self.free_port())
        gate = self.overview()["gate"]
        self.assertEqual((gate["status"], gate["listening"], gate["connected_clients"]), ("stopped", False, 0))

    def test_listening_gate_is_running_even_without_log_data(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            self.with_address(server.getsockname()[1])
            gate = self.overview()["gate"]
        self.assertEqual((gate["status"], gate["listening"]), ("running", True))

    def test_without_address_it_falls_back_to_the_log(self):
        self.write_audit([rec(stamp(8, 8), "SERVER_START")])
        gate = self.overview()["gate"]
        self.assertEqual((gate["status"], gate["listening"]), ("running", None))


class TestAccountMetrics(DashboardDataBase):
    def eval_state(self, **overrides):
        base = {
            "preset_id": "25k_flex_eval", "trading_day": "2026-10-08", "day_start_balance": 25000.0,
            "eod_hwm": 25000.0, "current_balance": 25200.0, "realized_pnl_today": 200.0,
            "qualifying_days_history": [],
            "closed_days": [{"date": "2026-10-07", "pnl": 100.0, "closing_balance": 25000.0, "qualified": False}],
        }
        base.update(overrides)
        return base

    def test_eval_account_metrics(self):
        self.write_state({"Sim101": self.eval_state()})
        acct = self.overview()["accounts"][0]
        self.assertEqual(acct["account"], "Sim101")
        self.assertEqual(acct["phase"], "eval")
        self.assertEqual(acct["liquidation_floor"], 24000.0)   # max(24.000, HWM 25.000 - 1.000)
        self.assertEqual(acct["drawdown_cushion"], 1200.0)     # 25.200 - 24.000
        self.assertEqual(acct["dll_remaining"], 600.0)
        self.assertEqual(acct["risk"], "ok")
        self.assertEqual(acct["eval"]["profit"], 200.0)
        self.assertEqual(acct["eval"]["profit_target"], 1250.0)
        self.assertEqual(acct["eval"]["consistency_cap"], 625.0)

    def test_danger_when_balance_reaches_liquidation_floor(self):
        self.write_state({"Sim101": self.eval_state(current_balance=24000.0, realized_pnl_today=-100.0)})
        self.assertEqual(self.overview()["accounts"][0]["risk"], "danger")

    def test_warn_when_daily_loss_is_close_to_the_limit(self):
        self.write_state({"Sim101": self.eval_state(current_balance=24600.0, realized_pnl_today=-450.0)})
        self.assertEqual(self.overview()["accounts"][0]["risk"], "warn")  # 450 de 600 de DLL (75%)

    def test_funded_account_reports_payout_progress(self):
        self.write_state({"Funded_25K_01": {
            "preset_id": "25k_flex_funded", "trading_day": "2026-10-08", "day_start_balance": 26000.0,
            "eod_hwm": 26000.0, "current_balance": 26150.0, "realized_pnl_today": 150.0,
            "qualifying_days_history": [110.0, 120.0], "closed_days": [],
        }})
        acct = self.overview()["accounts"][0]
        payout = acct["payout"]
        self.assertEqual(payout["balance_for_payout"], 27100.0)
        self.assertEqual(payout["balance_remaining"], 950.0)
        self.assertEqual(payout["qualified_days_count"], 3)      # 2 historicos + hoy
        self.assertEqual(payout["mode"], "accumulate_balance")
        self.assertEqual(acct["liquidation_floor"], 25000.0)    # min(26.000 - 1.000, 25.100) = 25.000

    def test_stale_state_day_is_flagged_as_pending_close(self):
        self.write_state({"Sim101": self.eval_state(trading_day="2026-10-07")})
        acct = self.overview()["accounts"][0]
        self.assertTrue(acct["pending_day_close"])
        self.assertEqual(acct["current_trading_day"], "2026-10-08")

    def test_unknown_preset_does_not_break_the_overview(self):
        self.write_state({"Mystery": self.eval_state(preset_id="does_not_exist")})
        acct = self.overview()["accounts"][0]
        self.assertFalse(acct["preset_known"])

    def test_closed_days_are_listed_and_filtered(self):
        self.write_state({"Sim101": self.eval_state()})
        self.assertEqual(len(self.overview()["closed_days"]), 1)
        self.assertEqual(self.overview(day="2026-10-07")["closed_days"][0]["account"], "Sim101")
        self.assertEqual(self.overview(day="2026-10-06")["closed_days"], [])
        self.assertEqual(self.overview(account="Other")["closed_days"], [])

    def test_missing_state_file_returns_no_accounts(self):
        self.assertEqual(self.overview()["accounts"], [])
        self.assertFalse(self.overview()["meta"]["state_file_found"])

    def test_account_filter_limits_the_account_cards(self):
        self.write_state({"Sim101": self.eval_state(), "Lucid_50K_01": self.eval_state(preset_id="50k_flex_eval")})
        self.assertEqual([a["account"] for a in self.overview(account="Lucid_50K_01")["accounts"]], ["Lucid_50K_01"])


if __name__ == "__main__":
    unittest.main()
