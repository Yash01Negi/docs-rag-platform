"""Structured JSON logging using only the standard library."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# Set per request by the request-ID middleware so every log line can be correlated.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes present on every LogRecord (plus uvicorn's ANSI-coloured duplicate message);
# anything else was passed via `extra=` and is included in the JSON output.
_RESERVED_ATTRS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    """Render each log record as a single JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Route application and uvicorn logs through a single JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Uvicorn installs its own handlers; let its records propagate to the root handler instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # Access logs are replaced by the structured request log in app.main.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
