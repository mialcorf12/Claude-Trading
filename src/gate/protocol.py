"""Protocolo JSON por linea (JSON-lines) para comunicacion TCP Python <-> NinjaTrader 8."""
from dataclasses import asdict, dataclass
import json
from typing import Any, Dict, Optional


class ProtocolError(Exception):
    """Error al parsear o validar un mensaje del protocolo."""
    pass


@dataclass
class Message:
    type: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict()) + "\n"


def decode_message(line: str) -> Dict[str, Any]:
    stripped = line.strip()
    if not stripped:
        raise ProtocolError("Mensaje vacio")
    try:
        data = json.loads(stripped)
    except Exception as e:
        raise ProtocolError(f"JSON invalido: {e}") from e

    if not isinstance(data, dict):
        raise ProtocolError("El mensaje debe ser un objeto JSON")

    msg_type = data.get("type")
    if not msg_type:
        raise ProtocolError("El mensaje no especifica el campo 'type'")

    return data


def encode_message(data: Dict[str, Any]) -> bytes:
    if "type" not in data:
        raise ProtocolError("El diccionario debe contener un campo 'type'")
    return (json.dumps(data) + "\n").encode("utf-8")
