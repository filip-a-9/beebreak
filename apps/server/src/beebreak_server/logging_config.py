"""Server log configuration: plain text (local default) or one JSON object per line.

The JSON formatter writes a fixed set of keys and nothing else, so no field can
carry user content (rule 12). For an exception it writes the class name and the
frames ("file:line:function"), never the exception text or locals: the text can
hold an email, a code or a token from the request.
"""

import contextvars
import json
import logging
import traceback
from datetime import UTC, datetime
from typing import Any

LOG_FORMATS = ("text", "json")

# Set by whatever assigns a request an id; the formatter adds it when present.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "beebreak_request_id", default=None
)


class JsonFormatter(logging.Formatter):
    """timestamp, level, logger, message and, when known, request_id."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            # Type and frames only: the exception text can hold an email, a code
            # or a token from the request, and locals can hold anything.
            exc = record.exc_info[1]
            line["exc_type"] = type(exc).__qualname__
            line["frames"] = [
                f"{frame.filename}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)
            ]
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            line["request_id"] = str(request_id)
        return json.dumps(line, ensure_ascii=True, separators=(",", ":"))


def build_log_config(log_format: str) -> dict[str, Any]:
    """dictConfig for uvicorn and the app. uvicorn's loggers stay at WARNING:
    they log client addresses and WebSocket query strings at INFO, so the app
    writes its own access lines (`beebreak_server.access`)."""
    if log_format not in LOG_FORMATS:
        raise ValueError("log format must be text or json")
    formatter: dict[str, Any] = (
        {"()": f"{__name__}.JsonFormatter"}
        if log_format == "json"
        else {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": formatter},
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["stderr"], "level": "WARNING", "propagate": False},
            "uvicorn.error": {"level": "WARNING"},
            "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
            "beebreak_server": {
                "handlers": ["stderr"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
