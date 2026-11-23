"""The single message format every channel is normalised into (and out of)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Channel = Literal["web", "whatsapp", "email", "voice"]


class InboundMessage(BaseModel):
    channel: Channel
    address: str = Field(description="Sender identity: web visitor id, phone number or email address")
    text: str
    session_id: str | None = None
    message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboundMessage(BaseModel):
    session_id: str
    channel: Channel
    address: str
    text: str
    status: Literal["answered", "pending_approval", "handoff", "blocked"] = "answered"
    citations: list[dict[str, Any]] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    handoff_id: str | None = None
    quick_replies: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    mode: str = "offline"
    metadata: dict[str, Any] = Field(default_factory=dict)
