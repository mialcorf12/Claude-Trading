"""Punto de entrada del dashboard: python -m src.dashboard.main [--config ...] [--host ...] [--port ...]"""
import argparse
import logging
from pathlib import Path
import sys

from src.dashboard.data import DashboardSources
from src.dashboard.launcher import GateLauncher
from src.dashboard.server import create_server
from src.gate.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Dashboard web (solo lectura) del Gate Lucid")
    parser.add_argument("--config", default="config/lucid_rules.yaml")
    parser.add_argument("--host", default="127.0.0.1", help="Por defecto solo local. 0.0.0.0 expone el dashboard SIN autenticacion")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--disable-start", action="store_true", help="Oculta/inhabilita el boton para arrancar el gate")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    config = load_config(args.config)
    sources = DashboardSources(
        state_path=Path(config.server.state_path),
        audit_path=Path(config.server.audit_log_path),
        config=config,
        gate_address=(config.server.host, config.server.port),
    )
    launcher = None
    if not args.disable_start:
        launcher = GateLauncher(
            config_path=str(Path(args.config).resolve()), console_log=Path("logs/gate_console.log"),
            host=config.server.host, port=config.server.port,
        )
    server = create_server(sources, args.host, args.port, launcher=launcher)

    logging.info("Dashboard en http://%s:%d  (estado: %s | auditoria: %s)", args.host, args.port,
                 sources.state_path.resolve(), sources.audit_path.resolve())
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        logging.warning("El dashboard no tiene autenticacion y esta escuchando en %s. Restringelo por firewall/VPN.", args.host)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
