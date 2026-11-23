"""Health, Prometheus metrics, and the built-in ops dashboard API (latency, cost, routing, traces)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from common.events import get_event_bus
from common.metrics import stats
from common.telemetry import ring_buffer
from rag.retriever import retriever_stats

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    runtime = request.app.state.runtime
    if not runtime.started:
        raise HTTPException(status_code=503, detail="starting")
    return {"status": "ready", "mode": runtime.mode, "tools": runtime.tools.tool_names}


@router.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/ops/api/stats")
async def ops_stats() -> dict:
    return stats.summary()


@router.get("/ops/api/traces")
async def traces(limit: int = 30) -> list[dict]:
    return ring_buffer.recent(limit)


@router.get("/ops/api/traces/{trace_id}")
async def trace(trace_id: str) -> list[dict]:
    spans = ring_buffer.get(trace_id)
    if not spans:
        raise HTTPException(status_code=404, detail="trace not found (only recent traces are kept in memory)")
    return spans


@router.get("/ops/api/rag")
async def rag_stats() -> dict:
    return retriever_stats()


@router.get("/ops/api/events")
async def events(limit: int = 50) -> list[dict]:
    return list(get_event_bus().recent)[-limit:][::-1]
