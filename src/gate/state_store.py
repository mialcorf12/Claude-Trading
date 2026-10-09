"""Persistencia en JSON del estado de las cuentas (sobrevive reinicios del gate).

Guarda baseline diario, HWM EOD y el historial de dias calificados. Escritura atomica (tmp + replace).
Un archivo ilegible se ignora (con warning) en vez de impedir el arranque del gate.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("gate.state_store")

STATE_VERSION = 1


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        """Devuelve {account: estado}; {} si el archivo no existe o es invalido."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            accounts = data.get("accounts", {})
            if not isinstance(accounts, dict):
                raise ValueError("'accounts' debe ser un objeto")
            return accounts
        except (OSError, ValueError) as exc:
            logger.warning("Estado persistido ilegible en %s (%s). Se ignora y se parte desde cero.", self.path, exc)
            return {}

    def save(self, accounts: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"version": STATE_VERSION, "accounts": accounts}
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.path)
