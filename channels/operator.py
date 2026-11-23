"""Operator API: approval queue for risky actions and the human handoff queue."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from agents.runtime import ApprovalError
from common.config import get_settings

router = APIRouter(prefix="/operator/api", tags=["operator"])


def operator_identity(
    authorization: str | None = Header(default=None), x_operator: str | None = Header(default=None)
) -> str:
    expected = f"Bearer {get_settings().operator_api_key}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="operator API key required")
    return x_operator or "duty.manager"


Operator = Depends(operator_identity)


class Decision(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class Reply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.get("/approvals")
async def approvals(request: Request, status: str | None = "pending", operator: str = Operator) -> list[dict]:
    return await request.app.state.runtime.hitl.list_approvals(status or None)


@router.post("/approvals/{approval_id}/approve")
async def approve(request: Request, approval_id: str, body: Decision, operator: str = Operator) -> dict:
    try:
        out = await request.app.state.runtime.resolve_approval(approval_id, True, operator, body.note)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "approved", "message_to_customer": out.text, "customer_status": out.status}


@router.post("/approvals/{approval_id}/reject")
async def reject(request: Request, approval_id: str, body: Decision, operator: str = Operator) -> dict:
    try:
        out = await request.app.state.runtime.resolve_approval(approval_id, False, operator, body.note)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "rejected", "message_to_customer": out.text}


@router.get("/handoffs")
async def handoffs(request: Request, status: str | None = None, operator: str = Operator) -> list[dict]:
    return await request.app.state.runtime.hitl.list_handoffs(status)


@router.post("/handoffs/{handoff_id}/claim")
async def claim(request: Request, handoff_id: str, operator: str = Operator) -> dict:
    row = await request.app.state.runtime.hitl.update_handoff(handoff_id, "claimed", operator)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.post("/handoffs/{handoff_id}/reply")
async def reply(request: Request, handoff_id: str, body: Reply, operator: str = Operator) -> dict:
    try:
        out = await request.app.state.runtime.operator_reply(handoff_id, body.text, operator)
    except ApprovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"delivered": True, "session_id": out.session_id}


@router.post("/handoffs/{handoff_id}/close")
async def close(request: Request, handoff_id: str, operator: str = Operator) -> dict:
    row = await request.app.state.runtime.hitl.update_handoff(handoff_id, "closed", operator)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.get("/sessions")
async def sessions(request: Request, operator: str = Operator) -> list[dict]:
    infos = await request.app.state.runtime.sessions.all_sessions()
    return [{"session_id": s.session_id, "channel": s.channel, "address": s.address, "last_seen": s.last_seen} for s in infos[:100]]


@router.get("/sessions/{session_id}")
async def session_detail(request: Request, session_id: str, operator: str = Operator) -> dict:
    runtime = request.app.state.runtime
    return {
        "session_id": session_id,
        "transcript": await runtime.transcript(session_id),
        "state": await runtime.session_state(session_id),
    }
