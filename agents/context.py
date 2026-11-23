"""Per-turn context handed to every specialist, plus formatting helpers."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from agents.schemas import AgentResult, RouterDecision, Slots
from agents.tools import ToolOutcome
from common.costs import TurnBudget

ACTION_LABELS = {
    "create_large_booking": "large booking",
    "cancel_with_refund": "cancellation and refund",
    "goodwill_refund": "refund request",
    "override_price": "price change",
}


@dataclass
class AgentContext:
    agent: str
    session_id: str
    turn_id: str
    channel: str
    customer_id: str | None
    user_text: str
    history: list[dict[str, Any]]
    slots: Slots
    flow: dict[str, Any]
    profile: dict[str, Any]
    decision: RouterDecision
    budget: TurnBudget
    restricted: bool = False
    approval: dict[str, Any] | None = None
    other_agents: list[str] = field(default_factory=list)


class Recorder:
    """Collects tool calls and payloads for the AgentResult (evals check these)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payloads: list[dict[str, Any]] = []

    def add(self, outcome: ToolOutcome) -> None:
        self.calls.append(outcome.log_entry())
        if outcome.payload:
            self.payloads.append(outcome.payload)

    def result(self, agent: str, text: str, **kwargs: Any) -> AgentResult:
        return AgentResult(agent=agent, text=text, tool_calls=self.calls, tool_payloads=self.payloads, **kwargs)


def new_approval_id() -> str:
    return "APR-" + secrets.token_hex(3).upper()


def make_pending(ctx: AgentContext, tool: str, args: dict[str, Any], payload: dict[str, Any], tool_use_id: str | None = None) -> dict[str, Any]:
    return {
        "approval_id": new_approval_id(),
        "agent": ctx.agent,
        "tool": tool,
        "args": {k: v for k, v in args.items() if k not in ("approval_token", "idempotency_key", "channel")},
        "action": payload.get("action", tool),
        "bound_params": payload.get("bound_params", {}),
        "reason": payload.get("reason") or payload.get("message", ""),
        "amount": payload.get("amount") or 0.0,
        "tool_use_id": tool_use_id,
    }


def fmt_date(value: str | date | None) -> str:
    if value is None:
        return "?"
    d = date.fromisoformat(value) if isinstance(value, str) else value
    return d.strftime("%a %d %b").replace(" 0", " ")


def money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "?"
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{value:,.2f}"


def stay_text(check_in: str | date, check_out: str | date) -> str:
    ci = date.fromisoformat(check_in) if isinstance(check_in, str) else check_in
    co = date.fromisoformat(check_out) if isinstance(check_out, str) else check_out
    nights = (co - ci).days
    return f"{fmt_date(ci)} to {fmt_date(co)} ({nights} night{'s' if nights != 1 else ''})"


def channel_note(channel: str) -> str:
    return {
        "voice": "Voice call: answer in one or two short spoken sentences. No lists, no markdown, no URLs.",
        "whatsapp": "WhatsApp: plain text, no markdown headings or tables, under 900 characters.",
        "email": "Email: complete sentences, a short greeting is fine, under 250 words.",
    }.get(channel, "Web chat: concise, plain text; a short list is fine for room options.")
