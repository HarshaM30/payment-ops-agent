"""Opaque keyset-pagination cursors (base64url-encoded JSON)."""

import base64
import binascii
import json
from typing import Any

from payops.errors import InvalidInputError

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def encode_cursor(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, required: set[str]) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise invalid_cursor() from exc
    if not isinstance(data, dict) or not required <= data.keys():
        raise invalid_cursor()
    return data


def invalid_cursor() -> InvalidInputError:
    return InvalidInputError(
        "Invalid cursor",
        [{"loc": ["query", "cursor"], "msg": "Invalid cursor", "type": "value_error"}],
    )
