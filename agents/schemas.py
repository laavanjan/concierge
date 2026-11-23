"""Typed contracts between graph nodes (also the JSON-schema outputs we ask models for)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal["booking", "pricing", "policy", "handoff", "smalltalk", "out_of_scope"]
BookingAction = Literal["new", "modify", "cancel", "lookup", "refund", "none"]
AgentName = Literal["booking", "pricing", "policy"]


class Slots(BaseModel):
    """Everything the customer has told us so far that a backend call might need."""

    model_config = ConfigDict(extra="ignore")

    check_in: date | None = None
    check_out: date | None = None
    nights: int | None = None
    guests: int | None = None
    rooms: int | None = None
    room_type: str | None = None
    reservation_id: str | None = None
    guest_name: str | None = None
    guest_email: str | None = None
    guest_phone: str | None = None
    promo_code: str | None = None
    refund_amount: float | None = None
    target_total: float | None = None

    def merged(self, update: Slots | dict[str, Any]) -> Slots:
        data = update if isinstance(update, dict) else update.model_dump(exclude_none=True)
        merged = {**self.model_dump(), **{k: v for k, v in data.items() if v is not None}}
        if "check_in" in data and "check_out" not in data and "nights" not in data and self.check_in and self.check_out:
            merged["nights"] = (self.check_out - self.check_in).days
            merged["check_out"] = None
        if "nights" in data and "check_out" not in data:
            merged["check_out"] = None
        return Slots.model_validate(merged)

    @property
    def resolved_check_out(self) -> date | None:
        if self.check_out:
            return self.check_out
        if self.check_in and self.nights:
            return self.check_in + timedelta(days=self.nights)
        return None

    @property
    def stay_complete(self) -> bool:
        return bool(self.check_in and self.resolved_check_out and self.guests)

    def compact(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True)
        if self.resolved_check_out and "check_out" not in data:
            data["check_out"] = self.resolved_check_out.isoformat()
        return data


class RouterDecision(BaseModel):
    """Supervisor output: which specialists handle this turn, and what was extracted."""

    intents: list[Intent] = Field(description="Ordered list of intents present in the latest customer message")
    booking_action: BookingAction = Field(
        default="none", description="For booking intents: new booking, modify, cancel, lookup or goodwill refund"
    )
    confirm: bool | None = Field(
        default=None, description="True if the customer accepts the assistant's last proposal, False if they decline"
    )
    policy_question: str | None = Field(
        default=None, description="Standalone rewrite of any question about hotel policies, facilities or FAQs"
    )
    slots: Slots = Field(default_factory=Slots)
    confidence: float = 1.0
    source: str = "rules"


class AgentResult(BaseModel):
    agent: str
    status: Literal["answered", "needs_approval", "handoff", "error"] = "answered"
    text: str = ""
    citations: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_payloads: list[dict[str, Any]] = Field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    flow: dict[str, Any] | None = None
    slots_update: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["llm", "offline", "degraded"] = "offline"
    handoff_reason: str | None = None
    retrieval: dict[str, Any] | None = None


class TurnResponse(BaseModel):
    text: str
    status: Literal["answered", "pending_approval", "handoff", "blocked"] = "answered"
    citations: list[dict[str, Any]] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    handoff_id: str | None = None
    violations: list[str] = Field(default_factory=list)
    quick_replies: list[str] = Field(default_factory=list)
