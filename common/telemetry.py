"""OpenTelemetry tracing.

Spans flow to any OTLP backend (Arize Phoenix, Langfuse, Jaeger, Tempo) when
OTEL_EXPORTER_OTLP_ENDPOINT is set. A small in-memory ring buffer also keeps
recent spans so the built-in /ops page can show a trace without extra infra.
The Anthropic SDK emits its own gen_ai spans under ours once a provider is set.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Status, StatusCode

from common.config import get_settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_configured = False


class RingBufferExporter(SpanExporter):
    """Keeps the most recent spans grouped by trace id (for the /ops trace view)."""

    def __init__(self, max_traces: int = 200) -> None:
        self.traces: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
        self.max_traces = max_traces

    def export(self, spans: Any) -> SpanExportResult:
        for span in spans:
            self._add(span)
        return SpanExportResult.SUCCESS

    def _add(self, span: ReadableSpan) -> None:
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x")
        record = {
            "name": span.name,
            "span_id": format(ctx.span_id, "016x"),
            "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
            "start": span.start_time,
            "end": span.end_time,
            "duration_ms": round(((span.end_time or 0) - (span.start_time or 0)) / 1e6, 2),
            "status": span.status.status_code.name,
            "attributes": {k: v for k, v in (span.attributes or {}).items()},
        }
        with _lock:
            bucket = self.traces.setdefault(trace_id, deque(maxlen=400))
            bucket.append(record)
            self.traces.move_to_end(trace_id)
            while len(self.traces) > self.max_traces:
                self.traces.popitem(last=False)

    def get(self, trace_id: str) -> list[dict[str, Any]]:
        with _lock:
            return sorted(self.traces.get(trace_id, []), key=lambda s: s["start"] or 0)

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        with _lock:
            items = list(self.traces.items())[-limit:]
        out = []
        for trace_id, spans in reversed(items):
            roots = [s for s in spans if s["parent_id"] is None] or list(spans)
            root = min(roots, key=lambda s: s["start"] or 0)
            out.append(
                {
                    "trace_id": trace_id,
                    "root": root["name"],
                    "spans": len(spans),
                    "duration_ms": root["duration_ms"],
                    "channel": root["attributes"].get("concierge.channel"),
                    "session_id": root["attributes"].get("concierge.session_id"),
                }
            )
        return out

    def shutdown(self) -> None:
        return None


ring_buffer = RingBufferExporter()


def setup_telemetry(service_name: str | None = None) -> None:
    global _configured
    with _lock:
        if _configured:
            return
        _configured = True
    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": service_name or settings.service_name}))
    provider.add_span_processor(SimpleSpanProcessor(ring_buffer))
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        log.info("OTLP trace export enabled -> %s", endpoint)
    trace.set_tracer_provider(provider)
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:  # pragma: no cover - optional instrumentation
        log.debug("httpx instrumentation unavailable", exc_info=True)


def instrument_fastapi(app: Any) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics,static")
    except Exception:  # pragma: no cover
        log.debug("fastapi instrumentation unavailable", exc_info=True)


def tracer() -> trace.Tracer:
    return trace.get_tracer("concierge")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    with tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value if isinstance(value, (str, bool, int, float)) else str(value))
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None
