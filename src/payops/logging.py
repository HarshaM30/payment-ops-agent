"""Structured JSON logging.

Every log line is one JSON object. Extra fields passed via ``extra={...}`` are
merged into the object, so request logs can carry method/path/status/latency.
"""

import json
import logging
import sys
from datetime import UTC, datetime

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._payops = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    # Replace only our own handler, so handlers added by others (e.g. pytest) survive.
    for existing in [h for h in root.handlers if getattr(h, "_payops", False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # Uvicorn's access log would duplicate our request log (and log query strings).
    logging.getLogger("uvicorn.access").disabled = True
