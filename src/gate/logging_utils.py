"""Utilidades de logging: timestamps en una zona horaria configurable (no UTC ni reloj del VPS)."""
from datetime import datetime, tzinfo
import logging


class TzFormatter(logging.Formatter):
    """Formatea %(asctime)s en la zona indicada, con abreviatura (CST/CDT)."""

    def __init__(self, fmt: str, tz: tzinfo):
        super().__init__(fmt)
        self.tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt=None) -> str:
        moment = datetime.fromtimestamp(record.created, self.tz)
        return f"{moment:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d} {moment.tzname()}"
