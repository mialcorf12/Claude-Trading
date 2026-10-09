"""Lanzador del gate desde el dashboard (boton "Arrancar gate").

Arranca `python -m src.gate.main` como proceso independiente (sobrevive al cierre del dashboard) con su salida en
un archivo de consola, y espera a que el puerto del gate empiece a escuchar. Un candado evita dobles arranques.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from src.dashboard.data import probe_port

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CONSOLE_TAIL_LINES = 8


class GateLauncher:
    def __init__(
        self,
        config_path: str,
        console_log: Path,
        cwd: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        probe: Optional[Callable[[], bool]] = None,
        popen: Callable[..., Any] = subprocess.Popen,
        is_windows: Optional[bool] = None,
        startup_timeout: float = 10.0,
        poll_interval: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        python: str = sys.executable,
    ):
        self.config_path = str(config_path)
        self.console_log = Path(console_log)
        self.cwd = cwd
        self._probe = probe or (lambda: probe_port(host, port))
        self._popen = popen
        self._is_windows = (sys.platform == "win32") if is_windows is None else is_windows
        self._startup_timeout = startup_timeout
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._now = now
        self._python = python
        self._lock = threading.Lock()
        self.process: Optional[Any] = None

    def is_listening(self) -> bool:
        return bool(self._probe())

    def command(self) -> List[str]:
        return [self._python, "-m", "src.gate.main", "--config", self.config_path]

    def _spawn_flags(self) -> Dict[str, Any]:
        if self._is_windows:
            return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}  # sin consola ni Ctrl+C heredado
        return {"start_new_session": True}

    def console_tail(self) -> List[str]:
        try:
            lines = self.console_log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [line for line in lines if line.strip()][-CONSOLE_TAIL_LINES:]

    @staticmethod
    def _result(ok: bool, status: str, message: str, pid: Optional[int] = None, tail: Optional[List[str]] = None) -> Dict[str, Any]:
        return {"ok": ok, "status": status, "pid": pid, "message": message, "console_tail": tail or []}

    def start(self) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return self._result(False, "busy", "Ya hay un arranque del gate en curso")
        try:
            if self.is_listening():
                return self._result(True, "already_running", "El gate ya esta en linea")

            self.console_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.console_log, "ab") as console:
                console.write(f"\n--- arranque desde el dashboard {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode("utf-8"))
                console.flush()
                self.process = self._popen(
                    self.command(), cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
                    **self._spawn_flags(),
                )

            deadline = self._now() + self._startup_timeout
            while self._now() < deadline:
                exit_code = self.process.poll()
                if exit_code is not None:
                    return self._result(
                        False, "failed", f"El gate termino al arrancar (codigo {exit_code}). Revisa logs/gate_console.log",
                        tail=self.console_tail(),
                    )
                if self.is_listening():
                    return self._result(True, "started", f"Gate arrancado (PID {self.process.pid})", pid=self.process.pid)
                self._sleep(self._poll_interval)
            return self._result(
                False, "failed", f"El gate no empezo a escuchar en {self._startup_timeout:.0f} s. Revisa logs/gate_console.log",
                pid=self.process.pid, tail=self.console_tail(),
            )
        finally:
            self._lock.release()
