"""Structured logging with trace correlation and PII masking."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from common.config import get_settings


class PiiMaskingFilter(logging.Filter):
    """Masks emails, phone numbers and card numbers before a record is written."""

    def filter(self, record: logging.LogRecord) -> bool:
        from guardrails.pii import mask_for_logs

        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = mask_for_logs(message)
        if masked != message:
            record.msg, record.args = masked, ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if ctx and ctx.trace_id:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")
        for key in ("session_id", "channel", "agent", "tool"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(PiiMaskingFilter())
    if settings.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "mcp", "uvicorn.access", "aiosqlite", "qdrant_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
