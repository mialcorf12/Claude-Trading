"""Servidor HTTP del dashboard: solo lectura (GET), sin dependencias externas.

Rutas: /  (UI), /static/<archivo de la lista blanca>, /api/overview?account=&day=, POST /api/gate/start
Escucha en 127.0.0.1 por defecto. No tiene autenticacion: no lo expongas a internet.

POST /api/gate/start es el unico endpoint que ejecuta algo (lanza el gate). Esta protegido contra CSRF y
DNS-rebinding: solo con el dashboard en loopback, Host permitido, Origin del mismo sitio y token por proceso
(cabecera X-Dashboard-Token; una pagina de otro origen no puede leerlo ni enviar esa cabecera sin preflight).
"""
from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import secrets
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from src.dashboard.data import DashboardSources, build_overview

logger = logging.getLogger("dashboard")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {  # lista blanca: nada fuera de este mapa se sirve desde disco
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
START_PATH = "/api/gate/start"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACCOUNT_RE = re.compile(r"^[\w .\-]{1,64}$")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
}


def parse_filters(query: str) -> Tuple[Optional[str], Optional[str]]:
    """Valida los filtros de la query. Lanza ValueError con un mensaje legible si son invalidos."""
    params = parse_qs(query)
    account = (params.get("account") or [""])[0].strip() or None
    day = (params.get("day") or [""])[0].strip() or None
    if account is not None and not ACCOUNT_RE.match(account):
        raise ValueError("Parametro 'account' invalido")
    if day is not None and not DAY_RE.match(day):
        raise ValueError("Parametro 'day' invalido (formato YYYY-MM-DD)")
    return account, day


def make_handler(sources: DashboardSources):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "GateDashboard/1.0"

        def _host_forbidden(self) -> bool:
            """En loopback solo se atiende con un Host propio (defensa contra DNS-rebinding, tambien en GET)."""
            server = self.server
            return bool(getattr(server, "enforce_host", False)) and self.headers.get("Host", "") not in server.allowed_hosts

        def _start_forbidden_reason(self) -> Optional[str]:
            """None si la peticion de arranque es legitima; si no, el motivo del rechazo."""
            server = self.server
            if getattr(server, "launcher", None) is None or not getattr(server, "start_enabled", False):
                return "El arranque desde la web esta deshabilitado (solo disponible con el dashboard en loopback)"
            if self.headers.get("Host", "") not in server.allowed_hosts:
                return "Host no permitido"
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in server.allowed_hosts}:
                return "Origen no permitido"
            if not hmac.compare_digest(self.headers.get("X-Dashboard-Token", ""), server.csrf_token):
                return "Token invalido"
            return None

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 (nombre impuesto por http.server)
            if self._host_forbidden():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Host no permitido"})
                return
            url = urlsplit(self.path)
            if url.path in STATIC_FILES:
                filename, content_type = STATIC_FILES[url.path]
                try:
                    self._send(HTTPStatus.OK, (STATIC_DIR / filename).read_bytes(), content_type)
                except OSError:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Archivo no encontrado"})
                return

            if url.path == START_PATH:
                self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Usa POST"})
                return

            if url.path == "/api/overview":
                try:
                    account, day = parse_filters(url.query)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                try:
                    payload = build_overview(sources, account=account, day=day)
                    start_enabled = bool(getattr(self.server, "start_enabled", False)) and self.server.launcher is not None
                    payload["meta"]["start_enabled"] = start_enabled
                    if start_enabled:
                        payload["meta"]["csrf_token"] = self.server.csrf_token
                    self._send_json(HTTPStatus.OK, payload)
                except Exception:  # un dato corrupto no debe tumbar el dashboard
                    logger.exception("Error construyendo el resumen")
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Error interno leyendo los datos"})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

        def _method_not_allowed(self) -> None:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Dashboard de solo lectura"})

        def do_POST(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != START_PATH:
                self._method_not_allowed()
                return
            reason = self._start_forbidden_reason()
            if reason is not None:
                logger.warning("Arranque del gate rechazado: %s", reason)
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "status": "forbidden", "message": reason, "console_tail": []})
                return
            try:
                result = self.server.launcher.start()
            except Exception:
                logger.exception("Error arrancando el gate")
                result = {"ok": False, "status": "failed", "message": "Error interno arrancando el gate", "console_tail": []}
            logger.info("Arranque del gate desde el dashboard: %s", result.get("status"))
            self._send_json(HTTPStatus.OK if result.get("ok") else HTTPStatus.INTERNAL_SERVER_ERROR, result)

        do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("%s - %s", self.address_string(), format % args)

    return DashboardHandler


def create_server(
    sources: DashboardSources, host: str = "127.0.0.1", port: int = 8780, launcher: Optional[Any] = None
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(sources))
    server.daemon_threads = True
    bound_port = server.server_address[1]
    server.launcher = launcher
    server.csrf_token = secrets.token_urlsafe(32)  # nuevo en cada arranque del dashboard
    server.start_enabled = launcher is not None and host in LOOPBACK_HOSTS
    server.enforce_host = host in LOOPBACK_HOSTS
    server.allowed_hosts = {f"127.0.0.1:{bound_port}", f"localhost:{bound_port}", f"[::1]:{bound_port}"}
    return server
