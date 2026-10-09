"""Tests del servidor HTTP del dashboard (solo lectura, lista blanca de rutas, validacion de filtros)."""
import http.client
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


class FakeLauncher:
    def __init__(self):
        self.starts = 0

    def start(self):
        self.starts += 1
        return {"ok": True, "status": "started", "pid": 99, "message": "Gate arrancado (PID 99)", "console_tail": []}


class TestStartGateEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.sources = DashboardSources(
            state_path=base / "s.json", audit_path=base / "a.log", config=load_config("config/lucid_rules.yaml"),
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_server(self, host="127.0.0.1", launcher="default"):
        self.launcher = FakeLauncher() if launcher == "default" else launcher
        server = create_server(self.sources, host, 0, launcher=self.launcher)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        return server

    def post(self, server, token="__ok__", host_header=None, origin=None, path="/api/gate/start"):
        port = server.server_address[1]
        headers = {"Host": host_header or f"127.0.0.1:{port}"}
        if token is not None:
            headers["X-Dashboard-Token"] = server.csrf_token if token == "__ok__" else token
        if origin is not None:
            headers["Origin"] = origin
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", path, body=b"", headers=headers)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, json.loads(body)

    def overview_meta(self, server):
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=5) as response:
            return json.loads(response.read())["meta"]

    def test_valid_request_starts_the_gate(self):
        server = self.run_server()
        status, body = self.post(server)
        self.assertEqual((status, body["status"]), (200, "started"))
        self.assertEqual(self.launcher.starts, 1)

    def test_missing_or_wrong_token_is_forbidden(self):
        server = self.run_server()
        for token in (None, "", "adivinando"):
            self.assertEqual(self.post(server, token=token)[0], 403, repr(token))
        self.assertEqual(self.launcher.starts, 0)

    def test_dns_rebinding_host_is_forbidden(self):
        server = self.run_server()
        status, _ = self.post(server, host_header="evil.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(self.launcher.starts, 0)

    def test_cross_origin_request_is_forbidden(self):
        server = self.run_server()
        status, _ = self.post(server, origin="http://evil.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(self.launcher.starts, 0)

    def test_same_origin_request_is_accepted(self):
        server = self.run_server()
        port = server.server_address[1]
        self.assertEqual(self.post(server, origin=f"http://127.0.0.1:{port}")[0], 200)

    def test_start_is_disabled_when_the_dashboard_listens_beyond_loopback(self):
        server = self.run_server(host="0.0.0.0")
        status, _ = self.post(server)
        self.assertEqual(status, 403)
        self.assertEqual(self.launcher.starts, 0)
        self.assertFalse(self.overview_meta(server)["start_enabled"])

    def test_start_is_disabled_without_a_launcher(self):
        server = self.run_server(launcher=None)
        self.assertEqual(self.post(server)[0], 403)
        self.assertFalse(self.overview_meta(server)["start_enabled"])

    def test_overview_meta_exposes_token_and_flag_only_when_enabled(self):
        server = self.run_server()
        meta = self.overview_meta(server)
        self.assertTrue(meta["start_enabled"])
        self.assertEqual(meta["csrf_token"], server.csrf_token)

    def test_loopback_server_rejects_foreign_host_on_get_too(self):
        server = self.run_server()
        port = server.server_address[1]
        for path in ("/api/overview", "/"):
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": "evil.example.com"})
            with self.assertRaises(urllib.error.HTTPError, msg=path) as ctx:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(ctx.exception.code, 403, path)

    def test_non_loopback_server_does_not_enforce_host(self):
        server = self.run_server(host="0.0.0.0")
        port = server.server_address[1]
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/overview", headers={"Host": "10.0.0.5:8780"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_get_on_the_start_path_is_405_and_other_posts_stay_405(self):
        server = self.run_server()
        port = server.server_address[1]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/gate/start", timeout=5)
        self.assertEqual(ctx.exception.code, 405)
        self.assertEqual(self.post(server, path="/api/overview")[0], 405)
        self.assertEqual(self.launcher.starts, 0)


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
