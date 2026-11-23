"""ConciergeRuntime: wires stores, MCP tools, models, retrieval and the compiled graph.

Channels call `handle()` with a normalised InboundMessage and get an
OutboundMessage back; operators call `resolve_approval()` / `operator_reply()`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from langgraph.types import Command

from agents.checkpoint import open_checkpointer
from agents.graph import build_graph
from agents.llm.factory import build_model
from agents.offline import OfflineAgents
from agents.router import Supervisor
from agents.schemas import TurnResponse
from agents.specialists import Specialists
from agents.stores import CustomerMemory, HitlStore, SessionInfo, make_session_store
from agents.tools import ToolGateway
from common.approvals import issue_token
from common.config import Settings, get_settings
from common.domain import domain
from common.messages import InboundMessage, OutboundMessage
from common.metrics import APPROVALS, TurnRecord, stats
from common.telemetry import current_trace_id, span
from guardrails.input_guard import CARD_WARNING, check_input

log = logging.getLogger(__name__)
Sender = Callable[[SessionInfo, OutboundMessage], Awaitable[None]]


class ApprovalError(Exception):
    pass


class ConciergeRuntime:
    def __init__(self, settings: Settings | None = None, tools: ToolGateway | None = None) -> None:
        self.settings = settings or get_settings()
        self.tools = tools or ToolGateway()
        self.sessions = make_session_store()
        self.memory = CustomerMemory()
        self.hitl = HitlStore()
        self.canary = "CNRY-" + hashlib.sha256(self.settings.approval_secret.encode()).hexdigest()[:10]
        self._senders: dict[str, list[Sender]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stack: AsyncExitStack | None = None
        self.graph: Any = None
        self.started = False

    # ------------------------------------------------------------------------------------ lifecycle
    async def start(self) -> ConciergeRuntime:
        if self.started:
            return self
        from rag.retriever import get_retriever

        self._stack = AsyncExitStack()
        await self.tools.start()
        self._stack.push_async_callback(self.tools.stop)
        retriever = await asyncio.to_thread(get_retriever)
        primary = build_model(self.settings.agent_model)
        fallback = build_model(self.settings.agent_fallback_model)
        router_model = build_model(self.settings.router_model)
        self.offline = OfflineAgents(self.tools, retriever)
        self.specialists = Specialists(self.tools, self.offline, primary, fallback, self.canary)
        self.supervisor = Supervisor(router_model)
        checkpointer = await self._stack.enter_async_context(open_checkpointer(self.settings))
        self.graph = build_graph(self).compile(checkpointer=checkpointer)
        self.mode = "llm" if primary else "offline"
        self.started = True
        log.info(
            "Concierge runtime ready: mode=%s agent_model=%s router=%s checkpointer=%s",
            self.mode, primary.model if primary else "offline", router_model.model if router_model else "rules",
            self.settings.checkpointer,
        )
        return self

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self.started = False

    def register_sender(self, channel: str, sender: Sender) -> None:
        self._senders.setdefault(channel, []).append(sender)

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    @staticmethod
    def _config(session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 40}

    async def pending_interrupt(self, session_id: str) -> dict | None:
        snapshot = await self.graph.aget_state(self._config(session_id))
        for task in snapshot.tasks or ():
            for intr in task.interrupts or ():
                return intr.value
        return None

    # ------------------------------------------------------------------------------------ turns
    async def handle(
        self, inbound: InboundMessage, on_event: Callable[[dict], Awaitable[None]] | None = None
    ) -> OutboundMessage:
        started = time.perf_counter()
        session = await self.sessions.get_or_create(inbound.channel, inbound.address, inbound.session_id)
        if not session.customer_id:
            session.customer_id = await self.memory.resolve(inbound.channel, inbound.address)
            await self.sessions.save(session)
        first_event_ms: float | None = None
        async with self._lock(session.session_id):
            with span(
                "concierge.turn", **{"concierge.channel": inbound.channel, "concierge.session_id": session.session_id}
            ) as current:
                trace_id = current_trace_id()
                config = self._config(session.session_id)
                takeover = await self.hitl.active_handoff(session.session_id)
                if takeover:
                    verdict = check_input(inbound.text)
                    await self.graph.aupdate_state(
                        config, {"transcript": [{"role": "user", "content": verdict.text}]}, as_node="finalize"
                    )
                    return self._outbound(
                        session, TurnResponse(text="", status="handoff", handoff_id=takeover["id"]), started, trace_id, []
                    )
                pending = await self.pending_interrupt(session.session_id)
                if pending:
                    response = TurnResponse(
                        text=(
                            f"Your earlier request (ref {pending['approval_id']}) is still with the duty manager, and I'll "
                            f"update you here as soon as they decide. For anything urgent, call the front desk on "
                            f"{domain().property.contact_phone}."
                        ),
                        status="pending_approval",
                        approval_id=pending["approval_id"],
                    )
                    return self._outbound(session, response, started, trace_id, [])
                verdict = check_input(inbound.text)
                payload = {
                    "session_id": session.session_id,
                    "channel": inbound.channel,
                    "customer_id": session.customer_id,
                    "turn_id": uuid.uuid4().hex[:12],
                    "user_text": verdict.text,
                    "guard": {
                        "action": verdict.action,
                        "score": verdict.injection.score,
                        "signals": verdict.injection.signals,
                        "findings": verdict.findings,
                    },
                }
                current.set_attribute("guard.action", verdict.action)
                if on_event is None:
                    state = await self.graph.ainvoke(payload, config)
                else:
                    state = {}
                    async for mode, chunk in self.graph.astream(payload, config, stream_mode=["custom", "values"]):
                        if mode == "custom":
                            if first_event_ms is None:
                                first_event_ms = (time.perf_counter() - started) * 1000
                            await on_event(chunk)
                        else:
                            state = chunk
                response = TurnResponse.model_validate(state.get("response") or {"text": "Sorry, something went wrong."})
                if verdict.card_masked:
                    response.text = f"{CARD_WARNING}\n\n{response.text}"
                current.set_attribute("turn.status", response.status)
                customer_id = state.get("customer_id") or session.customer_id
                if customer_id != session.customer_id:
                    session.customer_id = customer_id
                    await self.sessions.save(session)
                return self._outbound(
                    session, response, started, trace_id, state.get("results") or [], state.get("usage") or [], first_event_ms
                )

    def _outbound(
        self,
        session: SessionInfo,
        response: TurnResponse,
        started: float,
        trace_id: str | None,
        results: list[dict],
        usage: list[dict] | None = None,
        first_event_ms: float | None = None,
    ) -> OutboundMessage:
        usage = usage or []
        latency = (time.perf_counter() - started) * 1000
        cost = round(sum(u.get("cost_usd", 0.0) for u in usage), 6)
        modes = {r.get("mode") for r in results}
        mode = "degraded" if "degraded" in modes else ("llm" if "llm" in modes else "offline")
        booked = any(
            c["name"] == "create_booking" and c["status"] == "ok" for r in results for c in r.get("tool_calls", [])
        )
        stats.add(
            TurnRecord(
                channel=session.channel,
                session_id=session.session_id,
                latency_ms=round(latency, 1),
                status=response.status,
                cost_usd=cost,
                input_tokens=sum(u.get("input_tokens", 0) for u in usage),
                output_tokens=sum(u.get("output_tokens", 0) for u in usage),
                agents=response.agents,
                mode=mode,
                booking_completed=booked,
                first_event_ms=first_event_ms,
                trace_id=trace_id,
            )
        )
        return OutboundMessage(
            session_id=session.session_id,
            channel=session.channel,  # type: ignore[arg-type]
            address=session.address,
            text=response.text,
            status=response.status,
            citations=response.citations,
            agents=response.agents,
            approval_id=response.approval_id,
            handoff_id=response.handoff_id,
            trace_id=trace_id,
            latency_ms=round(latency, 1),
            cost_usd=cost,
            mode=mode,
            metadata={
                "violations": response.violations,
                "tool_calls": [c for r in results for c in r.get("tool_calls", [])],
                "usage": usage,
            },
        )

    # ------------------------------------------------------------------------------------ human in the loop
    async def resolve_approval(self, approval_id: str, approved: bool, operator: str, note: str | None = None) -> OutboundMessage:
        record = await self.hitl.get_approval(approval_id)
        if record is None:
            raise ApprovalError(f"approval {approval_id} not found")
        if record["status"] != "pending":
            raise ApprovalError(f"approval {approval_id} is already {record['status']}")
        session = await self.sessions.get(record["session_id"])
        if session is None:
            session = SessionInfo(record["session_id"], record["channel"], record["session_id"], record.get("customer_id"))
        started = time.perf_counter()
        async with self._lock(session.session_id):
            pending = await self.pending_interrupt(session.session_id)
            if not pending or pending.get("approval_id") != approval_id:
                await self.hitl.decide(approval_id, "expired", operator, "conversation is no longer waiting for this approval")
                raise ApprovalError("this conversation is no longer waiting for that approval")
            token = issue_token(record["action"], record["bound_params"], approval_id, operator) if approved else None
            await self.hitl.decide(approval_id, "approved" if approved else "rejected", operator, note)
            APPROVALS.labels(record["action"], "approved" if approved else "rejected").inc()
            with span("concierge.approval", **{"approval.id": approval_id, "approval.approved": approved}):
                trace_id = current_trace_id()
                state = await self.graph.ainvoke(
                    Command(resume={"approved": approved, "token": token, "operator": operator, "note": note}),
                    self._config(session.session_id),
                )
            outcome = (state.get("approval") or {}).get("outcome")
            if approved:
                await self.hitl.set_outcome(approval_id, outcome or {}, "executed" if (outcome or {}).get("status") == "ok" else "failed")
            response = TurnResponse.model_validate(state["response"])
            outbound = self._outbound(session, response, started, trace_id, state.get("results") or [], state.get("usage") or [])
        await self.deliver(session, outbound)
        return outbound

    async def operator_reply(self, handoff_id: str, text: str, operator: str) -> OutboundMessage:
        ticket = await self.hitl.get_handoff(handoff_id)
        if ticket is None:
            raise ApprovalError(f"handoff {handoff_id} not found")
        session = await self.sessions.get(ticket["session_id"]) or SessionInfo(
            ticket["session_id"], ticket["channel"], ticket["session_id"], ticket.get("customer_id")
        )
        await self.hitl.update_handoff(handoff_id, "claimed", operator)
        async with self._lock(session.session_id):
            await self.graph.aupdate_state(
                self._config(session.session_id),
                {"transcript": [{"role": "assistant", "content": text, "agents": ["operator"], "operator": operator}]},
                as_node="finalize",
            )
        outbound = OutboundMessage(
            session_id=session.session_id,
            channel=session.channel,  # type: ignore[arg-type]
            address=session.address,
            text=text,
            agents=["operator"],
            handoff_id=handoff_id,
        )
        await self.deliver(session, outbound)
        return outbound

    async def deliver(self, session: SessionInfo, outbound: OutboundMessage) -> None:
        await self.sessions.push(
            session.session_id,
            {"role": "assistant", "text": outbound.text, "status": outbound.status, "citations": outbound.citations,
             "agents": outbound.agents, "ts": time.time()},
        )
        for sender in self._senders.get(session.channel, []):
            try:
                await sender(session, outbound)
            except Exception:
                log.exception("delivery over %s failed for %s", session.channel, session.session_id)

    async def transcript(self, session_id: str) -> list[dict]:
        snapshot = await self.graph.aget_state(self._config(session_id))
        return list((snapshot.values or {}).get("transcript") or [])

    async def session_state(self, session_id: str) -> dict:
        snapshot = await self.graph.aget_state(self._config(session_id))
        values = snapshot.values or {}
        return {
            "slots": values.get("slots"),
            "flow": values.get("flow"),
            "profile": values.get("profile"),
            "pending_interrupt": await self.pending_interrupt(session_id),
            "next": list(snapshot.next or ()),
        }
