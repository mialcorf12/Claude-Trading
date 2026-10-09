"""Tests del lanzador del gate (boton "Arrancar gate"): sin spawns reales salvo el test de integracion final."""
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

import yaml

from src.dashboard.launcher import GateLauncher


class FakeProcess:
    def __init__(self, pid=4321, exit_code=None):
        self.pid = pid
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


class Recorder:
    """Popen falso que registra la llamada y simula que el gate empieza a escuchar tras N sondeos."""

    def __init__(self, process=None, listen_after_polls=2):
        self.calls = []
        self.process = process or FakeProcess()
        self.polls = 0
        self.listen_after_polls = listen_after_polls

    def popen(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.process

    def probe(self):
        self.polls += 1
        started = bool(self.calls)
        return started and self.polls > self.listen_after_polls


class LauncherTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.console = Path(self.tmp.name) / "logs" / "gate_console.log"
        self.recorder = Recorder()
        self.clock = [0.0]

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, probe=None, is_windows=False, startup_timeout=5.0, popen=None):
        return GateLauncher(
            config_path="config/lucid_rules.yaml", console_log=self.console, cwd=self.tmp.name,
            probe=probe or self.recorder.probe, popen=popen or self.recorder.popen, is_windows=is_windows,
            startup_timeout=startup_timeout, poll_interval=0.1,
            sleep=lambda seconds: self.clock.__setitem__(0, self.clock[0] + seconds), now=lambda: self.clock[0],
        )


class TestGateLauncher(LauncherTestBase):
    def test_does_nothing_when_the_gate_is_already_listening(self):
        result = self.make(probe=lambda: True).start()
        self.assertEqual((result["ok"], result["status"]), (True, "already_running"))
        self.assertEqual(self.recorder.calls, [])

    def test_spawns_the_gate_module_with_the_config_and_waits_until_it_listens(self):
        result = self.make().start()
        self.assertEqual((result["ok"], result["status"], result["pid"]), (True, "started", 4321))
        command, kwargs = self.recorder.calls[0]
        self.assertEqual(command, [sys.executable, "-m", "src.gate.main", "--config", "config/lucid_rules.yaml"])
        self.assertEqual(kwargs["cwd"], self.tmp.name)
        self.assertEqual(kwargs["stdin"], -3)  # subprocess.DEVNULL: sin consola interactiva

    def test_posix_spawn_is_detached_in_its_own_session(self):
        self.make(is_windows=False).start()
        kwargs = self.recorder.calls[0][1]
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("creationflags", kwargs)

    def test_windows_spawn_is_detached_from_the_dashboard_console(self):
        self.make(is_windows=True).start()
        kwargs = self.recorder.calls[0][1]
        self.assertEqual(kwargs["creationflags"], 0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        self.assertNotIn("start_new_session", kwargs)

    def test_output_goes_to_the_console_log(self):
        self.make().start()
        self.assertTrue(self.console.exists())

    def test_reports_failure_immediately_when_the_process_dies_and_shows_the_console_tail(self):
        self.recorder.process = FakeProcess(exit_code=1)

        def popen(command, **kwargs):
            self.console.parent.mkdir(parents=True, exist_ok=True)
            self.console.write_text("linea 1\nTraceback ...\nValueError: preset desconocido\n", encoding="utf-8")
            return self.recorder.popen(command, **kwargs)

        result = self.make(probe=lambda: False, popen=popen).start()
        self.assertEqual((result["ok"], result["status"]), (False, "failed"))
        self.assertIn("codigo 1", result["message"])
        self.assertEqual(result["console_tail"][-1], "ValueError: preset desconocido")
        self.assertLess(self.clock[0], 1.0)  # no espero el timeout completo si el proceso ya murio

    def test_reports_failure_when_the_gate_never_listens(self):
        result = self.make(probe=lambda: False, startup_timeout=1.0).start()
        self.assertEqual((result["ok"], result["status"]), (False, "failed"))
        self.assertIn("no empezo a escuchar", result["message"])
        self.assertGreaterEqual(self.clock[0], 1.0)

    def test_concurrent_clicks_spawn_only_one_process(self):
        gate = threading.Event()

        def slow_popen(command, **kwargs):
            gate.wait(2)
            return self.recorder.popen(command, **kwargs)

        launcher = self.make(popen=slow_popen)
        results = []
        first = threading.Thread(target=lambda: results.append(launcher.start()))
        first.start()
        time.sleep(0.1)
        second = launcher.start()  # mientras el primero sigue arrancando
        gate.set()
        first.join(3)

        self.assertEqual(second["status"], "busy")
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(results[0]["status"], "started")


class TestRealSpawn(unittest.TestCase):
    """Integracion: arranca el gate de verdad en un puerto libre con estado/logs en un directorio temporal."""

    def test_launcher_starts_a_real_gate_process(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            config = yaml.safe_load(Path("config/lucid_rules.yaml").read_text(encoding="utf-8"))
            config["server"].update(
                port=port, state_path=str(Path(tmp) / "state.json"), audit_log_path=str(Path(tmp) / "audit.log")
            )
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

            launcher = GateLauncher(
                config_path=str(config_path), console_log=Path(tmp) / "console.log", cwd=os.getcwd(),
                host="127.0.0.1", port=port, startup_timeout=15.0,
            )
            self.assertFalse(launcher.is_listening())
            result = launcher.start()
            try:
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["status"], "started")
                self.assertTrue(launcher.is_listening())
                self.assertEqual(launcher.start()["status"], "already_running")
            finally:
                if launcher.process is not None:
                    launcher.process.terminate()
                    launcher.process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
