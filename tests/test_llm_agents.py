"""LLM code paths with a scripted model: tool loop, approvals, fallbacks, citations, guardrails."""

from __future__ import annotations

import json

from agents.llm.base import LLMUnavailable
from agents.schemas import RouterDecision, Slots
from tests.fakes import ScriptedModel, last_tool_results, reply, tool_use

STAY = {"check_in": "2026-12-12", "check_out": "2026-12-15", "guests": 2}


def use_models(runtime, primary, fallback=None, router=None):
    runtime.specialists.primary = primary
    runtime.specialists.fallback = fallback
    if router is not None:
        runtime.supervisor.model = router


async def test_tool_loop_quotes_with_strict_tools_and_context(runtime, say):
    def after_search(messages):
        results = last_tool_results(messages)
        options = json.loads(results[0]["content"])["options"]
        assert any(o["room_type"] == "DLX" and o["bookable"] for o in options)
        return tool_use("get_quote", {"room_type": "DLX", **STAY})

    def after_quote(messages):
        quote = json.loads(last_tool_results(messages)[0]["content"])
        return reply(f"The Deluxe King Sea View comes to ${quote['total']:,.2f} including tax. Shall I book it?")

    model = ScriptedModel([tool_use("search_availability", STAY), after_search, after_quote])
    use_models(runtime, model)
    out = await say("book 12-15 Dec for 2, deluxe king")
    assert out.mode == "llm" and out.agents == ["booking"]
    assert "$727.65" in out.text and not out.metadata["violations"]
    first = model.calls[0]
    assert first["effort"] == "low" and "Internal marker" in first["system"]
    assert first["messages"][-1]["role"] == "system" and "today: 2026-11-24" in first["messages"][-1]["content"]
    names = {t["name"] for t in first["tools"]}
    assert "override_price" not in names and "create_booking" in names  # booking agent's allow-list
    for tool in first["tools"]:
        props = tool["input_schema"]["properties"]
        assert tool["strict"] and tool["input_schema"]["additionalProperties"] is False
        assert not {"approval_token", "idempotency_key", "channel"} & set(props)
    state = await runtime.session_state("web-test")
    assert state["flow"]["offer"]["total"] == 727.65  # derived from the tool result for degraded-mode continuity


async def test_approval_required_pauses_then_llm_writes_the_update(runtime, say):
    from services.client import BackendClient

    quote = await BackendClient().quote(room_type="STD", check_in="2026-12-12", check_out="2026-12-15", guests=2, rooms=1)
    booking = await BackendClient().create_reservation(
        {"quote_id": quote.data["quote_id"], "guest_name": "Alex Morgan", "guest_email": "alex@example.com"}, None
    )
    rid = booking.data["reservation_id"]

    def after_refund(messages):
        result = json.loads(last_tool_results(messages)[0]["content"])
        assert result["status"] == "approval_required" and "NOT been performed" in result["note"]
        return reply("I've sent your refund request to the duty manager and will update you here.")

    def update(messages):
        context = messages[-1]["content"]
        assert messages[-1]["role"] == "system" and "APPROVED" in context
        return reply("Good news: your $40.00 refund was approved.")

    model = ScriptedModel(
        [tool_use("request_refund", {"reservation_id": rid, "guest_email": "alex@example.com", "amount": 40, "reason": "noisy"}),
         after_refund, update]
    )
    use_models(runtime, model)
    paused = await say(f"refund me $40 on {rid} (alex@example.com), the room was noisy")
    assert paused.status == "pending_approval" and paused.approval_id
    final = await runtime.resolve_approval(paused.approval_id, True, "manager", None)
    assert final.status == "answered" and "approved" in final.text
    refreshed = await BackendClient().get_reservation(rid, "alex@example.com")
    assert refreshed.data["refunded"] == 40.0


async def test_model_fallback_then_degraded_mode(runtime, say):
    use_models(runtime, ScriptedModel([LLMUnavailable("503")]), ScriptedModel([reply("Hello from the fallback model.")]))
    out = await say("book 12-15 Dec for 2")
    assert out.mode == "llm" and out.text == "Hello from the fallback model."

    use_models(runtime, ScriptedModel([LLMUnavailable("503")]), ScriptedModel([LLMUnavailable("529")]))
    degraded = await say("the deluxe king please")
    assert degraded.mode == "degraded" and "$727.65" in degraded.text  # deterministic agent picked up the flow


async def test_refusal_hands_off(runtime, say):
    use_models(runtime, ScriptedModel([reply("", stop="refusal")]))
    out = await say("book 12-15 Dec for 2")
    assert out.status == "handoff" and out.handoff_id


async def test_hallucinated_booking_reference_is_blocked(runtime, say):
    use_models(runtime, ScriptedModel([reply("You're all set! Your reference is AUR-QQQQQQ, total $500.00.")]))
    out = await say("book 12-15 Dec for 2, deluxe king, Alex Morgan alex@example.com")
    assert "unverified_reservation_id" in out.metadata["violations"]
    assert "AUR-QQQQQQ" not in out.text and out.status == "handoff"


async def test_policy_answer_uses_native_citations(runtime, say):
    def answer(messages):
        docs = [b for b in messages[-1]["content"] if b.get("type") == "document"]
        assert docs and all(d["citations"] == {"enabled": True} for d in docs)
        pets = next(i for i, d in enumerate(docs) if "Pets" in d["title"])
        return reply(
            "Dogs up to 15 kg are welcome in Standard and Deluxe rooms for $30 per night.",
            citations=[{"type": "char_location", "cited_text": "Dogs up to 15 kg", "document_index": pets,
                        "document_title": docs[pets]["title"], "start_char_index": 0, "end_char_index": 16}],
        )

    use_models(runtime, ScriptedModel([answer]))
    out = await say("can I bring my dog?")
    assert out.citations[0]["id"] == "pets-and-children#pets" and out.text.endswith("[1]")


async def test_uncited_policy_answer_is_replaced_by_extractive(runtime, say):
    use_models(runtime, ScriptedModel([reply("Sure, bring any pet you like, it's free!")]))
    out = await say("can I bring my dog?")
    assert "any pet you like" not in out.text and out.citations and "15 kg" in out.text


async def test_llm_router_merges_with_rule_slots(runtime, say):
    parsed = RouterDecision(intents=["pricing"], slots=Slots(guests=3, check_in=None), source="llm")
    use_models(runtime, None, None, router=ScriptedModel(parsed=parsed, model="claude-haiku-5-5"))
    out = await say("what would 12-15 Dec cost for us three in the family suite?")
    assert out.agents == ["pricing"] and "Family Suite" in out.text
    state = await runtime.session_state("web-test")
    assert state["slots"]["check_in"] == "2026-12-12" and state["slots"]["guests"] == 3
    assert out.metadata["usage"] and out.metadata["usage"][0]["purpose"] == "router"
