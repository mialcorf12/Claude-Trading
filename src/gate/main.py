"""Punto de entrada principal para ejecutar el Gate de autorizacion Python."""
import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
import zoneinfo

from src.gate.config import load_config
from src.gate.logging_utils import TzFormatter
from src.gate.server import GateServer


def setup_logging(tz_name: str = "America/Chicago"):
    """Configura logs a consola con timestamps en la zona horaria indicada (no UTC)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        TzFormatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", zoneinfo.ZoneInfo(tz_name))
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


async def run_server(config_path: str):
    config = load_config(config_path)
    setup_logging(config.server.log_timezone)
    server = GateServer(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        logging.info("Senal de terminacion recibida. Deteniendo GateServer...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # En Windows loop.add_signal_handler no esta implementado para ciertos loops
            pass

    await server.start()
    logging.info("Gate listo. Presiona Ctrl+C para salir.")

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await server.stop()


def main():
    parser = argparse.ArgumentParser(description="Lucid Trading - Python Authorization Gate Server")
    parser.add_argument(
        "--config",
        default="config/lucid_rules.yaml",
        help="Ruta al archivo de configuracion yaml (default: config/lucid_rules.yaml)",
    )
    args = parser.parse_args()

    setup_logging()
    try:
        asyncio.run(run_server(args.config))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
