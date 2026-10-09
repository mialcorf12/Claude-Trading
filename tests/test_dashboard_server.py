"""Tests del servidor HTTP del dashboard (solo lectura, lista blanca de rutas, validacion de filtros)."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from src.dashboard.data import DashboardSources
from src.dashboard.server import create_server, parse_filters
from src.gate.config import load_config


class TestParseFilters(unittest.TestCase):
    def test_empty_query_means_no_filters(self):
        self.assertEqual(parse_filters(""), (None, None))

    def test_valid_filters(self):
        self.assertEqual(parse_filters("account=Sim101&day=2026-10-08"), ("Sim101", "2026-10-08"))

    def test_rejects_malformed_day_and_account(self):
        for query in ("day=2026-13-99x", "day=ayer", "account=" + "a" * 100, "account=%3Cscript%3E"):
            with self.assertRaises(ValueError, msg=query):
                parse_filters(query)


class TestDashboardHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        sources = DashboardSources(
            state_path=base / "gate_state.json", audit_path=base / "gate_audit.log",
            config=load_config("config/lucid_rules.yaml"),
        )
        (base / "gate_audit.log").write_text(
            json.dumps({"timestamp": "2026-10-08T09:00:00-05:00", "event": "SERVER_START", "data": {}}) + "\n",
            encoding="utf-8",
        )
        cls.server = create_server(sources, "127.0.0.1", 0)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_index_and_static_assets_are_served(self):
        for path, content_type in (("/", "text/html"), ("/static/app.js", "text/javascript"), ("/static/style.css", "text/css")):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(content_type), path)
            self.assertGreater(len(body), 100, path)

    def test_security_headers_present(self):
        _, headers, _ = self.get("/")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_overview_returns_json(self):
        status, _, body = self.get("/api/overview")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["gate"]["status"], "running")
        self.assertIn("summary", data)

    def test_overview_accepts_filters(self):
        status, _, body = self.get("/api/overview?account=Sim101&day=2026-10-08")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["meta"]["filters"], {"account": "Sim101", "day": "2026-10-08"})

    def test_invalid_filter_is_a_400(self):
        status, _, body = self.get("/api/overview?day=hoy")
        self.assertEqual(status, 400)
        self.assertIn("day", json.loads(body)["error"])

    def test_unknown_and_traversal_paths_are_404(self):
        for path in ("/nope", "/static/../../config/lucid_rules.yaml", "/static/app.js/../../state_store.py", "/state/gate_state.json"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_write_methods_are_rejected(self):
        for method in ("POST", "PUT", "DELETE"):
            request = urllib.request.Request(self.base_url + "/api/overview", data=b"{}", method=method)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(ctx.exception.code, 405, method)


if __name__ == "__main__":
    unittest.main()
