import json
import logging

from app.core.logging import JsonFormatter, request_id_var


def test_json_formatter_includes_extra_fields_and_request_id() -> None:
    record = logging.makeLogRecord(
        {"name": "app.test", "levelname": "INFO", "msg": "hello %s", "args": ("世界",)}
    )
    record.path = "/api/v1/health"

    token = request_id_var.set("req-42")
    try:
        line = JsonFormatter().format(record)
    finally:
        request_id_var.reset(token)

    payload = json.loads(line)
    assert payload["message"] == "hello 世界"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["request_id"] == "req-42"
    assert payload["path"] == "/api/v1/health"
    assert "timestamp" in payload
