"""Servidor HTTP del dashboard: solo lectura (GET), sin dependencias externas.

Rutas: /  (UI), /static/<archivo de la lista blanca>, /api/overview?account=&day=
Escucha en 127.0.0.1 por defecto. No tiene autenticacion: no lo expongas a internet.
"""
from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
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
            url = urlsplit(self.path)
            if url.path in STATIC_FILES:
                filename, content_type = STATIC_FILES[url.path]
                try:
                    self._send(HTTPStatus.OK, (STATIC_DIR / filename).read_bytes(), content_type)
                except OSError:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Archivo no encontrado"})
                return

            if url.path == "/api/overview":
                try:
                    account, day = parse_filters(url.query)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                try:
                    self._send_json(HTTPStatus.OK, build_overview(sources, account=account, day=day))
                except Exception:  # un dato corrupto no debe tumbar el dashboard
                    logger.exception("Error construyendo el resumen")
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Error interno leyendo los datos"})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

        def _method_not_allowed(self) -> None:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Dashboard de solo lectura"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("%s - %s", self.address_string(), format % args)

    return DashboardHandler


def create_server(sources: DashboardSources, host: str = "127.0.0.1", port: int = 8780) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(sources))
    server.daemon_threads = True
    return server
