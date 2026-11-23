"""Prometheus metrics plus a rolling in-memory window that backs the /ops dashboard."""

from __future__ import annotations

import statistics
import threading
import time
from collections import Counter as PyCounter
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from prometheus_client import Counter, Histogram

_LAT_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34)

TURN_LATENCY = Histogram(
    "concierge_turn_latency_seconds", "End-to-end reply latency per turn", ["channel", "status"], buckets=_LAT_BUCKETS
)
FIRST_EVENT_LATENCY = Histogram(
    "concierge_first_event_seconds", "Time to first streamed event (web)", ["channel"], buckets=_LAT_BUCKETS
)
LLM_LATENCY = Histogram("concierge_llm_latency_seconds", "LLM call latency", ["model", "purpose"], buckets=_LAT_BUCKETS)
LLM_TOKENS = Counter("concierge_llm_tokens_total", "LLM tokens", ["model", "kind"])
LLM_COST = Counter("concierge_llm_cost_usd_total", "LLM spend in USD", ["model"])
TOOL_CALLS = Counter("concierge_tool_calls_total", "MCP tool calls", ["tool", "status"])
TOOL_LATENCY = Histogram("concierge_tool_latency_seconds", "MCP tool latency", ["tool"], buckets=_LAT_BUCKETS)
GUARDRAIL_EVENTS = Counter("concierge_guardrail_events_total", "Guardrail decisions", ["stage", "kind"])
ROUTES = Counter("concierge_routes_total", "Supervisor routing decisions", ["agent"])
HANDOFFS = Counter("concierge_handoffs_total", "Operator handoffs", ["reason"])
APPROVALS = Counter("concierge_approvals_total", "Approval requests and decisions", ["action", "status"])
FALLBACKS = Counter("concierge_fallbacks_total", "Fallback activations", ["kind"])
BUDGET_EXCEEDED = Counter("concierge_budget_exceeded_total", "Turn budget overruns", ["kind"])
RETRIEVAL_LATENCY = Histogram("concierge_retrieval_seconds", "Hybrid retrieval latency", buckets=_LAT_BUCKETS)


@dataclass
class TurnRecord:
    channel: str
    session_id: str
    latency_ms: float
    status: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    agents: list[str] = field(default_factory=list)
    mode: str = "offline"
    booking_completed: bool = False
    first_event_ms: float | None = None
    trace_id: str | None = None
    ts: float = field(default_factory=time.time)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1], 1)


class StatsStore:
    def __init__(self, maxlen: int = 5000) -> None:
        self._records: deque[TurnRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, record: TurnRecord) -> None:
        with self._lock:
            self._records.append(record)
        TURN_LATENCY.labels(record.channel, record.status).observe(record.latency_ms / 1000)
        if record.first_event_ms is not None:
            FIRST_EVENT_LATENCY.labels(record.channel).observe(record.first_event_ms / 1000)

    def records(self) -> list[TurnRecord]:
        with self._lock:
            return list(self._records)

    def summary(self) -> dict[str, Any]:
        records = self.records()
        by_channel: dict[str, list[TurnRecord]] = {}
        for rec in records:
            by_channel.setdefault(rec.channel, []).append(rec)
        channels = {}
        for channel, recs in sorted(by_channel.items()):
            lat = [r.latency_ms for r in recs]
            first = [r.first_event_ms for r in recs if r.first_event_ms is not None]
            channels[channel] = {
                "turns": len(recs),
                "p50_ms": _pct(lat, 50),
                "p95_ms": _pct(lat, 95),
                "first_event_p50_ms": _pct(first, 50),
                "first_event_p95_ms": _pct(first, 95),
                "avg_cost_usd": round(sum(r.cost_usd for r in recs) / len(recs), 5),
            }
        bookings = sum(1 for r in records if r.booking_completed)
        total_cost = sum(r.cost_usd for r in records)
        return {
            "turns": len(records),
            "channels": channels,
            "total_cost_usd": round(total_cost, 4),
            "cost_per_completed_booking_usd": round(total_cost / bookings, 4) if bookings else None,
            "completed_bookings": bookings,
            "tokens": {
                "input": sum(r.input_tokens for r in records),
                "output": sum(r.output_tokens for r in records),
            },
            "status": dict(PyCounter(r.status for r in records)),
            "agents": dict(PyCounter(a for r in records for a in r.agents)),
            "modes": dict(PyCounter(r.mode for r in records)),
            "recent": [asdict(r) for r in records[-25:]][::-1],
        }


stats = StatsStore()
