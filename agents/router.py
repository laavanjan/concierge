"""Supervisor: decide which specialists handle a turn.

Cost-aware routing: the deterministic rules run on every turn (free, precise
for dates and references); the small model (Claude Haiku or a local Qwen via
Ollama) refines intents when configured. If the small model fails, the rules
decision is used, so routing never blocks a turn.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.llm.base import ChatModel, LLMError
from agents.nlu import rule_decision
from agents.prompts import render
from agents.schemas import RouterDecision
from common.clock import today
from common.domain import domain
from common.metrics import FALLBACKS

log = logging.getLogger(__name__)


def _router_input(text: str, history: list[dict[str, Any]], flow: dict[str, Any]) -> str:
    recent = history[-4:]
    lines = [f"{m['role']}: {str(m['content'])[:300]}" for m in recent]
    pending = flow.get("awaiting")
    return (
        f"<context>\ntoday: {today().isoformat()} ({today().strftime('%A')})\n"
        f"assistant is waiting for: {pending or 'nothing'}\n"
        f"recent conversation:\n" + ("\n".join(lines) or "(none)") + "\n</context>\n"
        f"<latest_customer_message>\n{text}\n</latest_customer_message>"
    )


def merge_decisions(rules: RouterDecision, llm: RouterDecision) -> RouterDecision:
    """Model decides intents; regex-precise slots (dates, ids, emails) win over model guesses."""
    if "handoff" in rules.intents or not llm.intents:
        return rules
    slots = llm.slots.merged(rules.slots.model_dump(exclude_none=True))
    intents = list(dict.fromkeys(llm.intents))
    action = llm.booking_action if llm.booking_action != "none" else rules.booking_action
    if "booking" not in intents:
        action = "none"
    return RouterDecision(
        intents=intents,
        booking_action=action,
        confirm=llm.confirm if llm.confirm is not None else rules.confirm,
        policy_question=llm.policy_question or rules.policy_question,
        slots=slots,
        confidence=llm.confidence,
        source=llm.source if llm.source.startswith("llm") else "llm",
    )


class Supervisor:
    def __init__(self, model: ChatModel | None) -> None:
        self.model = model
        pack = domain()
        self.system, self.prompt_ref = render(
            "router", property_name=pack.property.name, room_codes=", ".join(r.code for r in pack.room_types)
        )

    async def decide(
        self, text: str, history: list[dict[str, Any]], flow: dict[str, Any], has_offer: bool
    ) -> tuple[RouterDecision, list[dict[str, Any]]]:
        rules = rule_decision(text, today(), flow, has_offer)
        if self.model is None or rules.intents == ["handoff"]:
            return rules, []
        try:
            parsed, response = await self.model.parse(
                system=self.system,
                messages=[{"role": "user", "content": _router_input(text, history, flow)}],
                schema=RouterDecision,
                max_tokens=1200,
                purpose="router",
            )
        except LLMError as exc:
            log.warning("router model failed (%s); using rules", exc)
            FALLBACKS.labels("router_rules").inc()
            return rules, []
        decision = merge_decisions(rules, parsed)  # type: ignore[arg-type]
        decision.source = f"llm:{self.model.model}"
        return decision, [response.usage_record("router")]
