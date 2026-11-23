"""Web chat API: request/response, Server-Sent Events with live progress, and an async outbox for polling."""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from common.messages import InboundMessage

router = APIRouter(tags=["web"])


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    visitor_id: str | None = None


def _inbound(body: ChatRequest) -> InboundMessage:
    visitor = body.visitor_id or body.session_id or uuid.uuid4().hex[:12]
    session_id = body.session_id or f"web-{visitor}"
    return InboundMessage(channel="web", address=visitor, text=body.text, session_id=session_id)


@router.post("/v1/chat")
async def chat(request: Request, body: ChatRequest) -> dict:
    out = await request.app.state.runtime.handle(_inbound(body))
    return out.model_dump()


@router.post("/v1/chat/stream")
async def chat_stream(request: Request, body: ChatRequest) -> StreamingResponse:
    runtime = request.app.state.runtime
    queue: asyncio.Queue = asyncio.Queue()

    async def on_event(event: dict) -> None:
        await queue.put(("progress", event))

    async def run() -> None:
        try:
            out = await runtime.handle(_inbound(body), on_event=on_event)
            await queue.put(("message", out.model_dump()))
        except Exception as exc:  # surface a clean error event to the widget
            await queue.put(("error", {"detail": type(exc).__name__}))
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())

    async def events():
        try:
            while (item := await queue.get()) is not None:
                kind, data = item
                yield f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get("/v1/sessions/{session_id}/messages")
async def outbox(request: Request, session_id: str, after: int = 0) -> dict:
    """Messages pushed after the fact (approval outcomes, operator replies)."""
    messages = await request.app.state.runtime.sessions.messages(session_id, after)
    return {"session_id": session_id, "messages": messages}


@router.get("/v1/sessions/{session_id}/transcript")
async def transcript(request: Request, session_id: str) -> dict:
    return {"session_id": session_id, "transcript": await request.app.state.runtime.transcript(session_id)}
