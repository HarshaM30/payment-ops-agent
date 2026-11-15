import json
import logging

from payops.logging import JsonFormatter


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    record.path = "/runs"
    out = json.loads(JsonFormatter().format(record))
    assert out["msg"] == "hello world"
    assert out["path"] == "/runs"
    assert out["level"] == "INFO"
