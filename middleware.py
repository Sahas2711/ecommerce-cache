"""
middleware.py — Production-grade observability layer.

Provides:
  • Structured JSON logging (compatible with CloudWatch Logs Insights)
  • WSGI middleware that measures wall-clock latency per request
  • Request ID injection (for distributed tracing correlation)
"""

import json
import logging
import time
import uuid
from typing import Callable


# ---------------------------------------------------------------------------
# Structured JSON log formatter
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """
    Emits each log record as a single JSON line.
    CloudWatch Logs Insights can query these fields natively.

    Example output:
    {
      "timestamp": "2024-11-15T14:32:01.123Z",
      "level": "INFO",
      "logger": "product_service",
      "message": "CACHE HIT  | key=prod:v1:product:detail:42",
      "extra": {}
    }
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level"    : record.levelname,
            "logger"   : record.name,
            "message"  : record.getMessage(),
        }
        # Attach any extra fields passed to the logger
        extra = {
            k: v for k, v in record.__dict__.items()
            if k not in logging.LogRecord(
                "", 0, "", 0, "", (), None
            ).__dict__ and k not in ("message", "asctime")
        }
        if extra:
            log_entry["extra"] = extra

        return json.dumps(log_entry)


def setup_logging(level: str = "INFO") -> None:
    """
    Replace the root logger's handlers with a JSON formatter writing to stdout.
    CloudWatch agent picks up stdout automatically on EC2/ECS.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# Request timing WSGI middleware
# ---------------------------------------------------------------------------

class RequestTimingMiddleware:
    """
    Measures wall-clock latency for every request and logs it as a structured
    JSON line. Also injects a unique X-Request-ID header so log lines from
    the same request can be correlated in CloudWatch Logs Insights.

    Example log line:
    {
      "timestamp": "...",
      "level": "INFO",
      "logger": "middleware",
      "message": "REQUEST_COMPLETE",
      "extra": {
        "request_id": "a1b2c3d4",
        "method": "GET",
        "path": "/products/42",
        "status": 200,
        "latency_ms": 4.12,
        "cache_status": "HIT"
      }
    }
    """

    def __init__(self, wsgi_app) -> None:
        self._app    = wsgi_app
        self._logger = logging.getLogger("middleware")

    def __call__(self, environ, start_response):
        request_id  = str(uuid.uuid4())[:8]
        request_start = time.perf_counter()

        environ["HTTP_X_REQUEST_ID"] = request_id

        captured = {}

        def _start_response(status, headers, exc_info=None):
            captured["status"] = int(status.split(" ")[0])
            headers.append(("X-Request-ID", request_id))
            return start_response(status, headers, exc_info)

        result = self._app(environ, _start_response)
        elapsed_ms = (time.perf_counter() - request_start) * 1000

        self._logger.info(
            "REQUEST_COMPLETE",
            extra={
                "request_id": request_id,
                "method"    : environ.get("REQUEST_METHOD"),
                "path"      : environ.get("PATH_INFO"),
                "status"    : captured.get("status"),
                "latency_ms": round(elapsed_ms, 3),
            },
        )

        return result
