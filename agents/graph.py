"""The orchestrator graph (LangGraph).

    START -> ingest -> route --(Send, in parallel)--> booking_agent | pricing_agent | policy_agent -> join
                         |                                                                          |
                         +--> compose (blocked / smalltalk / handoff)       interim <-- pending approval?
                                  |                                            |
                                  v                                       approval_gate (interrupt)
                               finalize -> END          resume --> back to the owning agent --> join -> compose

Why a graph and not one big prompt: explicit state, checkpoints per session,
parallel specialists, and a real pause/resume point for human approval.
"""

from __future__ import annotations

import contextlib
import logging
import operator
import uuid
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Overwrite, Send, interrupt

from agents.context import AgentContext
from agents.schemas import RouterDecision, Slots, TurnResponse
from agents.stores import extract_preferences
from common.config import get_settings
from common.costs import TurnBudget
from common.domain import domain
from common.metrics import HANDOFFS, ROUTES
from guardrails.input_guard import SAFE_REFUSAL
from guardrails.output_guard import Evidence, check_output

if TYPE_CHECKING:
    from agents.runtime import ConciergeRuntime

log = logging.getLogger(__name__)
AGENTS = ("booking", "pricing", "policy")


def merge_dicts(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


class GraphState(TypedDict, total=False):
    session_id: str
    channel: str
    customer_id: str | None
    turn_id: str
    user_text: str
    guard: dict
    transcript: Annotated[list[dict], operator.add]
    slots: dict
    flow: dict
    profile: dict
    router: dict
    plan: list[str]
    task: str
    results: Annotated[list[dict], operator.add]
    usage: Annotated[list[dict], operator.add]
    pending_action: dict | None
    approval: dict | None
    response: dict | None
    delivered: list[str]
    security_strikes: int


def emit(stage: str, **data: Any) -> None:
    with contextlib.suppress(Exception):  # not streaming (ainvoke) or outside a run
        get_stream_writer()({"type": "progress", "stage": stage, **data})


def latest_results(state: GraphState) -> list[dict]:
    """Most recent result per agent this turn, in plan order, skipping ones already delivered."""
    latest: dict[str, dict] = {}
    for r in state.get("results") or []:
        latest[r["agent"]] = r
    delivered = set(state.get("delivered") or [])
    order = list(state.get("plan") or []) + [a for a in latest if a not in (state.get("plan") or [])]
    return [latest[a] for a in order if a in latest and latest[a]["result_id"] not in delivered]


SMALLTALK = {
    "greeting": "Hi! I'm the {name} assistant. I can book or change a stay, give you prices, or answer questions about "
    "the hotel. What can I do for you?",
    "thanks": "You're welcome! Anything else I can help with?",
    "out_of_scope": "I can only help with stays at {name}: bookings, prices and hotel policies. Is there something "
    "like that I can help with?",
}


def build_graph(rt: ConciergeRuntime) -> StateGraph:
    settings = get_settings()

    async def ingest(state: GraphState) -> dict:
        emit("received")
        guard = state.get("guard") or {}
        updates: dict[str, Any] = {
            "results": Overwrite([]),
            "usage": Overwrite([]),
            "pending_action": None,
            "approval": None,
            "response": None,
            "delivered": [],
            "plan": [],
            "router": {},
            "profile": await rt.memory.profile(state.get("customer_id")),
        }
        if guard.get("action") == "block":
            strikes = (state.get("security_strikes") or 0) + 1
            updates["security_strikes"] = strikes
            updates["transcript"] = [{"role": "user", "content": "[message withheld by the safety filter]", "turn_id": state["turn_id"]}]
            response = TurnResponse(text=SAFE_REFUSAL, status="blocked", violations=guard.get("signals", []))
            if strikes >= 3:
                ticket = await rt.hitl.create_handoff(
                    state["session_id"], state["channel"], state.get("customer_id"), "security_review",
                    f"{strikes} messages blocked by the prompt-injection filter in this session.",
                )
                response.handoff_id = ticket["id"]
                HANDOFFS.labels("security_review").inc()
            updates["response"] = response.model_dump()
            return updates
        updates["transcript"] = [{"role": "user", "content": state["user_text"], "turn_id": state["turn_id"]}]
        return updates

    def after_ingest(state: GraphState) -> str:
        return "compose" if state.get("response") else "route"

    async def route(state: GraphState) -> dict:
        emit("routing")
        flow = state.get("flow") or {}
        history = (state.get("transcript") or [])[:-1]
        decision, usage = await rt.supervisor.decide(state["user_text"], history, flow, bool(flow.get("offer")))
        slots = Slots.model_validate(state.get("slots") or {}).merged(decision.slots)
        plan = [i for i in dict.fromkeys(decision.intents) if i in AGENTS]
        if "booking" in plan and "pricing" in plan:
            plan.remove("pricing")
        updates: dict[str, Any] = {
            "router": decision.model_dump(mode="json"),
            "plan": plan,
            "slots": slots.model_dump(mode="json"),
            "usage": usage,
        }
        d = decision.slots
        if state.get("customer_id") and (d.guest_email or d.guest_phone or d.guest_name):
            customer_id = await rt.memory.link_contact(
                state["customer_id"], email=d.guest_email, phone=d.guest_phone, name=d.guest_name
            )
            updates["customer_id"] = customer_id
            updates["profile"] = await rt.memory.profile(customer_id)  # returning guests get their history now
        for agent in plan:
            ROUTES.labels(agent).inc()
        name = domain().property.name
        if "handoff" in decision.intents:
            ROUTES.labels("handoff").inc()
            HANDOFFS.labels("customer_request").inc()
            ticket = await rt.hitl.create_handoff(
                state["session_id"], state["channel"], state.get("customer_id"), "customer_request",
                summarize_for_operator(state),
            )
            updates["response"] = TurnResponse(
                text="Of course. I've asked a member of our team to join this conversation; they'll reply here shortly.",
                status="handoff",
                handoff_id=ticket["id"],
            ).model_dump()
        elif not plan:
            kind = "out_of_scope" if "out_of_scope" in decision.intents else (
                "thanks" if state["user_text"].strip().lower().startswith(("thank", "cheers", "ty")) else "greeting"
            )
            ROUTES.labels(kind).inc()
            updates["response"] = TurnResponse(text=SMALLTALK[kind].format(name=name)).model_dump()
        return updates

    def dispatch(state: GraphState) -> list[Send] | str:
        if state.get("response"):
            return "compose"
        return [Send(f"{agent}_agent", {**state, "task": agent}) for agent in state.get("plan") or []]

    def make_agent(agent: str):
        async def run_agent(state: GraphState) -> dict:
            emit("agent", agent=agent)
            approval = state.get("approval")
            if approval and approval.get("agent") != agent:
                approval = None
            router = RouterDecision.model_validate(state.get("router") or {"intents": [agent]})
            transcript = state.get("transcript") or []
            ctx = AgentContext(
                agent=agent,
                session_id=state["session_id"],
                turn_id=state["turn_id"],
                channel=state["channel"],
                customer_id=state.get("customer_id"),
                user_text=state["user_text"],
                history=transcript if approval else transcript[:-1],
                slots=Slots.model_validate(state.get("slots") or {}),
                flow=state.get("flow") or {},
                profile=state.get("profile") or {},
                decision=router,
                budget=TurnBudget(settings.turn_latency_budget_ms, settings.turn_cost_budget_usd),
                restricted=(state.get("guard") or {}).get("action") == "flag",
                approval=approval,
                other_agents=[a for a in (state.get("plan") or []) if a != agent],
            )
            result, usage = await rt.specialists.run(ctx, question=router.policy_question if agent == "policy" else None)
            for call in result.tool_calls:
                emit("tool", agent=agent, tool=call["name"], status=call["status"])
            record = result.model_dump(mode="json")
            record["result_id"] = uuid.uuid4().hex[:12]
            return {"results": [record], "usage": usage}

        run_agent.__name__ = f"{agent}_agent"
        return run_agent

    async def join(state: GraphState) -> dict:
        flow = dict(state.get("flow") or {})
        slots = Slots.model_validate(state.get("slots") or {})
        pending = None
        for result in latest_results(state):
            if result.get("flow") is not None:
                flow = result["flow"]
            if result.get("slots_update"):
                data = {**slots.model_dump(), **result["slots_update"]}
                slots = Slots.model_validate(data)
            if result["status"] == "needs_approval" and result.get("pending_action") and pending is None:
                pending = result["pending_action"]
        return {"flow": flow, "slots": slots.model_dump(mode="json"), "pending_action": pending}

    def after_join(state: GraphState) -> str:
        return "interim" if state.get("pending_action") else "compose"

    async def interim(state: GraphState) -> dict:
        pending = state["pending_action"]
        await rt.hitl.create_approval(pending, state["session_id"], state["channel"], state.get("customer_id"))
        response, delivered = await build_response(state, rt)
        response.status = "pending_approval"
        response.approval_id = pending["approval_id"]
        emit("approval_requested", approval_id=pending["approval_id"], action=pending["action"])
        return {
            "response": response.model_dump(),
            "delivered": [*(state.get("delivered") or []), *delivered],
            "transcript": [assistant_entry(state, response)],
        }

    async def approval_gate(state: GraphState) -> Command:
        pending = state["pending_action"]
        decision = interrupt(
            {
                "type": "approval_required",
                "approval_id": pending["approval_id"],
                "action": pending["action"],
                "reason": pending.get("reason"),
                "amount": pending.get("amount"),
            }
        )
        approved = bool(decision.get("approved"))
        outcome = None
        if approved:
            result = await rt.tools.call(
                pending["tool"],
                pending["args"],
                agent=pending["agent"],
                session_id=state["session_id"],
                turn_id=state["turn_id"],
                channel=state["channel"],
                approval_token=decision.get("token"),
            )
            outcome = {"status": result.status, "payload": result.payload, "error": result.error}
        approval = {
            **pending,
            "approved": approved,
            "operator": decision.get("operator"),
            "note": decision.get("note"),
            "outcome": outcome,
        }
        return Command(goto=f"{pending['agent']}_agent", update={"approval": approval, "pending_action": None})

    async def compose(state: GraphState) -> dict:
        emit("composing")
        if state.get("response") and not state.get("results"):
            return {}
        response, delivered = await build_response(state, rt)
        return {"response": response.model_dump(), "delivered": [*(state.get("delivered") or []), *delivered]}

    async def finalize(state: GraphState) -> dict:
        response = TurnResponse.model_validate(state["response"])
        updates: dict[str, Any] = {"transcript": [assistant_entry(state, response)]}
        if state.get("customer_id") and (state.get("guard") or {}).get("action") != "block":
            facts = extract_preferences(state.get("user_text", ""))
            if facts:
                await rt.memory.remember(state["customer_id"], facts, source=f"{state['channel']}:{state['session_id']}")
        emit("done", status=response.status)
        return updates

    graph = StateGraph(GraphState)
    graph.add_node("ingest", ingest)
    graph.add_node("route", route)
    for agent in AGENTS:
        graph.add_node(f"{agent}_agent", make_agent(agent))
    graph.add_node("join", join)
    graph.add_node("interim", interim)
    graph.add_node("approval_gate", approval_gate, destinations=tuple(f"{a}_agent" for a in AGENTS))
    graph.add_node("compose", compose)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "ingest")
    graph.add_conditional_edges("ingest", after_ingest, ["route", "compose"])
    graph.add_conditional_edges("route", dispatch, [*(f"{a}_agent" for a in AGENTS), "compose"])
    for agent in AGENTS:
        graph.add_edge(f"{agent}_agent", "join")
    graph.add_conditional_edges("join", after_join, ["interim", "compose"])
    graph.add_edge("interim", "approval_gate")
    graph.add_edge("compose", "finalize")
    graph.add_edge("finalize", END)
    return graph


def assistant_entry(state: GraphState, response: TurnResponse) -> dict:
    return {
        "role": "assistant",
        "content": response.text,
        "agents": response.agents,
        "status": response.status,
        "citations": response.citations,
        "turn_id": state.get("turn_id"),
    }


def summarize_for_operator(state: GraphState) -> str:
    lines = [f"{m['role']}: {str(m['content'])[:200]}" for m in (state.get("transcript") or [])[-8:]]
    slots = {k: v for k, v in (state.get("slots") or {}).items() if v}
    return "Recent conversation:\n" + "\n".join(lines) + (f"\nKnown details: {slots}" if slots else "")


async def build_response(state: GraphState, rt: ConciergeRuntime) -> tuple[TurnResponse, list[str]]:
    results = latest_results(state)
    texts, citations, agents, delivered = [], [], [], []
    handoff_reason = None
    evidence = Evidence()
    for result in results:
        delivered.append(result["result_id"])
        agents.append(result["agent"])
        if result.get("status") == "handoff" and not handoff_reason:
            handoff_reason = result.get("handoff_reason") or "agent_handoff"
        text = result.get("text", "").strip()
        if result.get("citations"):
            offset = len(citations)
            for i in range(len(result["citations"]), 0, -1):
                text = text.replace(f"[{i}]", f"[{i + offset}]")
            citations.extend(result["citations"])
        if text:
            texts.append(text)
        for payload in result.get("tool_payloads") or []:
            evidence.add_payload(payload)
        if result.get("flow"):
            evidence.add_payload(result["flow"])
    flow = state.get("flow") or {}
    evidence.add_payload(flow)
    evidence.add_payload(state.get("slots") or {})
    evidence.add_payload((state.get("approval") or {}).get("outcome") or {})
    evidence.add_payload(state.get("user_text", ""))
    pack = domain()
    evidence.allowed_emails |= {pack.property.contact_email.lower(), "privacy@aurora-bay.example"}
    for key in ("guest_email",):
        if (state.get("slots") or {}).get(key):
            evidence.allowed_emails.add(state["slots"][key].lower())
    if (state.get("profile") or {}).get("email"):
        evidence.allowed_emails.add(state["profile"]["email"].lower())
    text = "\n\n".join(texts) or "Sorry, I didn't quite get that. Could you rephrase?"
    verdict = check_output(text, evidence, canary=rt.canary, system_prompts=rt.specialists.system_prompts())
    response = TurnResponse(
        text=verdict.text,
        citations=citations if not verdict.blocked else [],
        agents=agents,
        violations=verdict.violations,
    )
    if verdict.blocked and "unverified_reservation_id" in verdict.violations:
        handoff_reason = handoff_reason or "unverified_reference"
    if handoff_reason:
        ticket = await rt.hitl.create_handoff(
            state["session_id"], state["channel"], state.get("customer_id"), handoff_reason, summarize_for_operator(state)
        )
        HANDOFFS.labels(handoff_reason.split(":")[0]).inc()
        response.status = "handoff"
        response.handoff_id = ticket["id"]
    return response, delivered
