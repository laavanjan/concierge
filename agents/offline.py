"""Deterministic specialists.

They run when no model is configured (CI, demos without keys) and as the
degraded-mode fallback when every model is unavailable, so a model outage
degrades the experience instead of breaking bookings. They call exactly the
same MCP tools as the LLM agents.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from agents.context import (
    ACTION_LABELS,
    AgentContext,
    Recorder,
    fmt_date,
    make_pending,
    money,
    stay_text,
)
from agents.schemas import AgentResult
from agents.tools import ToolGateway, ToolOutcome
from common.clock import today, utcnow
from rag.retriever import HybridRetriever, RetrievalResult
from rag.text import tokenize

TOOL_TROUBLE = (
    "I'm having trouble reaching our reservation system right now, so I've passed your request to the front desk "
    "team. They'll pick it up here shortly."
)
FORBIDDEN_TEXT = "I can't make changes from this conversation right now. I can connect you with the front desk if you'd like."


def _offer_from_quote(q: dict[str, Any]) -> dict[str, Any]:
    labels = sorted({a["label"] for n in q.get("nightly", []) for a in n.get("adjustments", [])})
    return {
        "quote_id": q.get("quote_id"),
        "room_type": q["room_type"],
        "room_name": q["room_name"],
        "check_in": q["check_in"],
        "check_out": q["check_out"],
        "guests": q["guests"],
        "rooms": q["rooms"],
        "total": q["total"],
        "subtotal": q.get("subtotal"),
        "tax": q.get("tax"),
        "currency": q.get("currency", "USD"),
        "expires_at": q.get("expires_at"),
        "promo": (q.get("promo") or {}).get("code") if (q.get("promo") or {}).get("status") == "applied" else None,
        "promo_status": q.get("promo"),
        "discounts": q.get("discounts", []),
        "adjustments": labels,
    }


def _offer_matches(offer: dict[str, Any], ctx: AgentContext) -> bool:
    s = ctx.slots
    co = s.resolved_check_out
    return (
        (not s.check_in or s.check_in.isoformat() == offer["check_in"])
        and (not co or co.isoformat() == offer["check_out"])
        and (not s.guests or s.guests == offer["guests"])
        and (not s.room_type or s.room_type == offer["room_type"])
        and (not s.rooms or s.rooms == offer["rooms"])
    )


def _quote_text(q: dict[str, Any]) -> str:
    rooms = f"{q['rooms']} x " if q["rooms"] > 1 else ""
    text = (
        f"The {rooms}{q['room_name']} for {stay_text(q['check_in'], q['check_out'])}, {q['guests']} "
        f"guest{'s' if q['guests'] != 1 else ''}, comes to {money(q['total'], q.get('currency', 'USD'))} including tax."
    )
    promo = q.get("promo") or {}
    if promo.get("status") == "applied":
        text += f" That includes promo {promo['code']}."
    elif promo.get("status"):
        text += f" ({promo.get('message')})"
    los = [d for d in q.get("discounts", []) if d["rule"] == "length_of_stay"]
    if los:
        text += f" You also get the {los[0]['label']} discount."
    return text


class OfflineAgents:
    def __init__(self, tools: ToolGateway, retriever: HybridRetriever | None) -> None:
        self.tools = tools
        self.retriever = retriever

    async def _call(self, ctx: AgentContext, rec: Recorder, name: str, **args: Any) -> ToolOutcome:
        outcome = await self.tools.call(
            name,
            {k: (v.isoformat() if isinstance(v, date) else v) for k, v in args.items() if v is not None},
            agent=ctx.agent,
            session_id=ctx.session_id,
            turn_id=ctx.turn_id,
            channel=ctx.channel,
            restricted=ctx.restricted,
        )
        rec.add(outcome)
        return outcome

    def _trouble(self, ctx: AgentContext, rec: Recorder, outcome: ToolOutcome) -> AgentResult:
        if outcome.status == "forbidden":
            return rec.result(ctx.agent, FORBIDDEN_TEXT)
        return rec.result(ctx.agent, TOOL_TROUBLE, status="handoff", handoff_reason=f"tool_failure:{outcome.name}")

    # ------------------------------------------------------------------------------------- booking
    async def booking(self, ctx: AgentContext) -> AgentResult:
        if ctx.approval:
            return self.after_approval(ctx)
        flow = dict(ctx.flow or {})
        action = ctx.decision.booking_action
        if action == "none":
            action = flow.get("action") or "new"
        handlers = {
            "new": self._new_booking,
            "modify": self._modify,
            "cancel": self._cancel,
            "lookup": self._lookup,
            "refund": self._refund,
        }
        return await handlers.get(action, self._new_booking)(ctx, flow)

    async def _new_booking(self, ctx: AgentContext, flow: dict[str, Any]) -> AgentResult:
        rec, s = Recorder(), ctx.slots
        offer = flow.get("offer")
        confirm = ctx.decision.confirm
        new_details = bool(ctx.decision.slots.guest_name or ctx.decision.slots.guest_email)
        base_flow = {"agent": "booking", "action": "new"}

        if offer and flow.get("awaiting") in ("confirm_booking", "guest_details"):
            if confirm is False:
                return rec.result(
                    "booking",
                    "No problem, I won't book that. Would you like different dates or another room?",
                    flow={**base_flow, "awaiting": None, "offer": None},
                )
            if _offer_matches(offer, ctx) and (confirm or new_details or flow.get("awaiting") == "guest_details"):
                missing = [label for label, value in (("full name", s.guest_name), ("email address", s.guest_email)) if not value]
                if missing:
                    return rec.result(
                        "booking",
                        f"Great! To complete the booking I just need the lead guest's {' and '.join(missing)}.",
                        flow={**base_flow, "awaiting": "guest_details", "offer": offer},
                    )
                if not offer.get("quote_id"):
                    offer = None
                else:
                    outcome = await self._call(
                        ctx, rec, "create_booking",
                        quote_id=offer["quote_id"], guest_name=s.guest_name, guest_email=s.guest_email,
                        guest_phone=s.guest_phone,
                    )
                    if outcome.ok:
                        r = outcome.payload
                        return rec.result(
                            "booking",
                            f"You're booked! Your reference is {r['reservation_id']}: {r['room_name']}, "
                            f"{stay_text(r['check_in'], r['check_out'])}, {r['guests']} guest{'s' if r['guests'] != 1 else ''}, "
                            f"total {money(r['total'], r['currency'])}. A confirmation and a secure payment link are on "
                            f"their way to {r['guest_email']}.",
                            flow={"agent": "booking", "action": None, "awaiting": None, "offer": None},
                            slots_update={"reservation_id": r["reservation_id"]},
                        )
                    if outcome.status == "approval_required":
                        pending = make_pending(ctx, "create_booking", outcome.args, outcome.payload)
                        return rec.result(
                            "booking",
                            f"Because this is a large booking ({money(offer['total'])}), a duty manager needs to approve "
                            f"it before it's confirmed. I've sent it over (ref {pending['approval_id']}) and I'll update "
                            "you here as soon as it's decided.",
                            status="needs_approval",
                            pending_action=pending,
                            flow={**base_flow, "awaiting": None, "offer": offer},
                        )
                    if outcome.status in ("quote_expired", "quote_not_found", "not_found"):
                        offer = None  # fall through to a fresh quote
                    elif outcome.status == "unavailable":
                        return rec.result(
                            "booking",
                            f"Sorry, the {offer['room_name']} has just sold out for those dates. Would you like me to "
                            "check other rooms or dates?",
                            flow={**base_flow, "awaiting": None, "offer": None},
                        )
                    elif outcome.status == "invalid":
                        return rec.result(
                            "booking",
                            f"I couldn't complete the booking: {outcome.payload.get('message', 'some details look invalid')}. "
                            "Could you check the name and email?",
                            flow={**base_flow, "awaiting": "guest_details", "offer": offer},
                        )
                    else:
                        return self._trouble(ctx, rec, outcome)

        check_in, check_out = s.check_in, s.resolved_check_out
        missing = []
        if not check_in:
            missing.append("your arrival date")
        if not check_out:
            missing.append("how many nights you'll stay")
        if not s.guests:
            missing.append("how many guests")
        if missing:
            ask = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " and " + missing[-1]
            return rec.result(
                "booking", f"I'd be happy to help you book. Could you tell me {ask}?",
                flow={**base_flow, "awaiting": "stay_details", "offer": None},
            )
        if check_in < today():
            return rec.result(
                "booking", f"{fmt_date(check_in)} has already passed. Which dates would you like instead?",
                flow={**base_flow, "awaiting": "stay_details", "offer": None},
            )
        rooms = s.rooms or 1
        room = s.room_type
        avail = await self._call(
            ctx, rec, "search_availability",
            check_in=check_in, check_out=check_out, guests=s.guests, rooms=rooms, room_type=room,
        )
        if not avail.ok:
            if avail.status == "invalid":
                return rec.result("booking", f"I couldn't check those dates: {avail.payload.get('message', '')}",
                                  flow={**base_flow, "awaiting": "stay_details"})
            return self._trouble(ctx, rec, avail)
        options = [o for o in avail.payload.get("options", []) if o.get("bookable")]
        if room and not options:
            room_name = next((o["name"] for o in avail.payload.get("options", [])), room)
            fits = next((o.get("fits_party") for o in avail.payload.get("options", [])), True)
            reason = "is sold out for those dates" if fits else f"doesn't fit {s.guests} guests in {rooms} room(s)"
            return rec.result(
                "booking", f"Sorry, the {room_name} {reason}. Shall I check the other room types?",
                flow={**base_flow, "awaiting": "room_choice", "offer": None},
                slots_update={"room_type": None},
            )
        if not options:
            return rec.result(
                "booking",
                f"Sorry, nothing fits {s.guests} guests for {stay_text(check_in, check_out)}. Would you like to try "
                "other dates, or more than one room?",
                flow={**base_flow, "awaiting": "stay_details", "offer": None},
            )
        if not room and len(options) > 1:
            lines = [f"- {o['name']}: {money(o.get('indicative_total'))} total" for o in options]
            return rec.result(
                "booking",
                f"Here's what's available for {stay_text(check_in, check_out)}, {s.guests} guest"
                f"{'s' if s.guests != 1 else ''} (totals include tax):\n" + "\n".join(lines) + "\nWhich room would you like?",
                flow={**base_flow, "awaiting": "room_choice", "offer": None},
            )
        room = room or options[0]["room_type"]
        quote = await self._call(
            ctx, rec, "get_quote",
            room_type=room, check_in=check_in, check_out=check_out, guests=s.guests, rooms=rooms, promo_code=s.promo_code,
        )
        if not quote.ok:
            if quote.status == "invalid":
                return rec.result("booking", quote.payload.get("message", "That combination doesn't work.") + " Shall I suggest another room?",
                                  flow={**base_flow, "awaiting": "room_choice"})
            return self._trouble(ctx, rec, quote)
        offer = _offer_from_quote(quote.payload)
        ask = (
            f"Shall I book it under {s.guest_name} ({s.guest_email})?"
            if s.guest_name and s.guest_email
            else "Shall I book it? Just reply with the lead guest's full name and email to confirm."
        )
        return rec.result(
            "booking", f"{_quote_text(quote.payload)} {ask}",
            flow={**base_flow, "awaiting": "confirm_booking", "offer": offer},
            slots_update={"room_type": room},
        )

    async def _need_reference(self, ctx: AgentContext, rec: Recorder, action: str) -> AgentResult | None:
        s = ctx.slots
        if s.reservation_id and s.guest_email:
            return None
        missing = []
        if not s.reservation_id:
            missing.append("your booking reference (it starts with AUR-)")
        if not s.guest_email:
            missing.append("the email address you booked with")
        verb = {"modify": "change", "cancel": "cancel", "lookup": "look up", "refund": "help with"}[action]
        return rec.result(
            "booking", f"I can {verb} that for you. Please send {' and '.join(missing)}.",
            flow={"agent": "booking", "action": action, "awaiting": "booking_ref"},
        )

    async def _lookup(self, ctx: AgentContext, flow: dict[str, Any]) -> AgentResult:
        rec = Recorder()
        if (ask := await self._need_reference(ctx, rec, "lookup")) is not None:
            return ask
        s = ctx.slots
        outcome = await self._call(ctx, rec, "get_booking", reservation_id=s.reservation_id, guest_email=s.guest_email)
        if outcome.status == "not_found":
            return rec.result("booking", "I couldn't find a booking with that reference and email. Could you double-check both?",
                              flow={"agent": "booking", "action": "lookup", "awaiting": "booking_ref"},
                              slots_update={"reservation_id": None})
        if not outcome.ok:
            return self._trouble(ctx, rec, outcome)
        r = outcome.payload
        return rec.result(
            "booking",
            f"Booking {r['reservation_id']} ({r['booking_status']}): {r['room_name']} for {r['guests']} guest"
            f"{'s' if r['guests'] != 1 else ''}, {stay_text(r['check_in'], r['check_out'])}, total "
            f"{money(r['total'], r['currency'])}.",
            flow={"agent": "booking", "action": None, "awaiting": None},
        )

    async def _modify(self, ctx: AgentContext, flow: dict[str, Any]) -> AgentResult:
        rec, s, confirm = Recorder(), ctx.slots, ctx.decision.confirm
        if (ask := await self._need_reference(ctx, rec, "modify")) is not None:
            return ask
        base = {"agent": "booking", "action": "modify"}
        change = flow.get("change") if flow.get("action") == "modify" else None
        if change and flow.get("awaiting") == "confirm_modify":
            if confirm is False:
                return rec.result("booking", "Okay, I've left your booking as it is.", flow={"agent": "booking", "action": None, "awaiting": None})
            if confirm:
                outcome = await self._call(ctx, rec, "modify_booking", **change)
                if outcome.ok:
                    r = outcome.payload
                    diff = r.get("price_difference", 0.0)
                    settle = (
                        f"The difference of {money(diff)} will be charged to your original payment method."
                        if diff > 0
                        else (f"{money(-diff)} will be refunded to your original payment method." if diff < 0 else "The price is unchanged.")
                    )
                    return rec.result(
                        "booking",
                        f"Done! Booking {r['reservation_id']} is now {r['room_name']}, {stay_text(r['check_in'], r['check_out'])}, "
                        f"total {money(r['total'])}. {settle}",
                        flow={"agent": "booking", "action": None, "awaiting": None},
                    )
                if outcome.status == "modification_window_closed":
                    return rec.result(
                        "booking",
                        "Changes within 48 hours of arrival have to go through the front desk. Would you like me to connect you?",
                        flow={"agent": "booking", "action": None, "awaiting": None},
                    )
                if outcome.status in ("unavailable", "invalid"):
                    return rec.result("booking", f"I couldn't make that change: {outcome.payload.get('message', 'not available')}.",
                                      flow={**base, "awaiting": "new_dates"})
                return self._trouble(ctx, rec, outcome)
        current = await self._call(ctx, rec, "get_booking", reservation_id=s.reservation_id, guest_email=s.guest_email)
        if current.status == "not_found":
            return rec.result("booking", "I couldn't find a booking with that reference and email. Could you double-check both?",
                              flow={**base, "awaiting": "booking_ref"}, slots_update={"reservation_id": None})
        if not current.ok:
            return self._trouble(ctx, rec, current)
        r = current.payload
        if r["booking_status"] != "confirmed":
            return rec.result("booking", f"Booking {r['reservation_id']} is {r['booking_status']}, so it can't be changed.",
                              flow={"agent": "booking", "action": None, "awaiting": None})
        d = ctx.decision.slots
        old_ci, old_co = date.fromisoformat(r["check_in"]), date.fromisoformat(r["check_out"])
        new_ci = d.check_in
        new_co = d.check_out or (new_ci + timedelta(days=d.nights) if new_ci and d.nights else None)
        if new_ci and not new_co:
            new_co = new_ci + (old_co - old_ci)
        if not new_ci and d.nights:
            new_ci, new_co = old_ci, old_ci + timedelta(days=d.nights)
        if not new_ci and new_co:
            new_ci = old_ci
        new_room = d.room_type if d.room_type and d.room_type != r["room_type"] else None
        new_guests = d.guests if d.guests and d.guests != r["guests"] else None
        if not (new_ci or new_room or new_guests):
            return rec.result(
                "booking",
                f"Your booking {r['reservation_id']} is {r['room_name']}, {stay_text(r['check_in'], r['check_out'])}. "
                "What would you like to change: the dates, the room type or the number of guests?",
                flow={**base, "awaiting": "new_dates"},
            )
        ci, co = new_ci or old_ci, new_co or old_co
        quote = await self._call(
            ctx, rec, "get_quote",
            room_type=new_room or r["room_type"], check_in=ci, check_out=co, guests=new_guests or r["guests"], rooms=r["rooms"],
        )
        if not quote.ok:
            if quote.status == "invalid":
                return rec.result("booking", quote.payload.get("message", "That change isn't possible."), flow={**base, "awaiting": "new_dates"})
            return self._trouble(ctx, rec, quote)
        q = quote.payload
        diff = round(q["total"] - r["total"], 2)
        delta = f"{money(abs(diff))} more" if diff > 0 else (f"{money(abs(diff))} less" if diff < 0 else "the same price")
        change = {
            "reservation_id": r["reservation_id"],
            "guest_email": s.guest_email,
            "check_in": ci.isoformat(),
            "check_out": co.isoformat(),
            "room_type": new_room,
            "guests": new_guests,
        }
        return rec.result(
            "booking",
            f"Changing {r['reservation_id']} to {q['room_name']}, {stay_text(ci, co)} would make the new total "
            f"{money(q['total'])}, which is {delta}. Shall I make the change?",
            flow={**base, "awaiting": "confirm_modify", "change": {k: v for k, v in change.items() if v is not None}},
        )

    async def _cancel(self, ctx: AgentContext, flow: dict[str, Any]) -> AgentResult:
        rec, s, confirm = Recorder(), ctx.slots, ctx.decision.confirm
        if (ask := await self._need_reference(ctx, rec, "cancel")) is not None:
            return ask
        base = {"agent": "booking", "action": "cancel"}
        if flow.get("action") == "cancel" and flow.get("awaiting") == "confirm_cancel":
            if confirm is False:
                return rec.result("booking", "Okay, your booking stays as it is.", flow={"agent": "booking", "action": None, "awaiting": None})
            if confirm:
                outcome = await self._call(
                    ctx, rec, "cancel_booking", reservation_id=s.reservation_id, guest_email=s.guest_email, reason="guest request"
                )
                if outcome.ok:
                    terms = outcome.payload.get("cancellation", {})
                    refund = terms.get("refund_amount", 0.0)
                    refund_text = (
                        f"{money(refund)} will be refunded to your original payment method within 5 to 10 business days."
                        if refund > 0
                        else "Under the policy no refund is due."
                    )
                    return rec.result(
                        "booking", f"Your booking {s.reservation_id} is cancelled. {refund_text}",
                        flow={"agent": "booking", "action": None, "awaiting": None},
                    )
                if outcome.status == "approval_required":
                    pending = make_pending(ctx, "cancel_booking", outcome.args, outcome.payload)
                    return rec.result(
                        "booking",
                        f"Because the refund is {money(pending['amount'])}, a duty manager needs to approve this "
                        f"cancellation. I've sent it over (ref {pending['approval_id']}) and I'll update you here as soon "
                        "as it's decided. Your booking stays active until then.",
                        status="needs_approval",
                        pending_action=pending,
                        flow={"agent": "booking", "action": None, "awaiting": None},
                    )
                if outcome.status == "not_active":
                    return rec.result("booking", "That booking is already cancelled.", flow={"agent": "booking", "action": None, "awaiting": None})
                return self._trouble(ctx, rec, outcome)
        preview = await self._call(ctx, rec, "preview_cancellation", reservation_id=s.reservation_id, guest_email=s.guest_email)
        if preview.status == "not_found":
            return rec.result("booking", "I couldn't find a booking with that reference and email. Could you double-check both?",
                              flow={**base, "awaiting": "booking_ref"}, slots_update={"reservation_id": None})
        if preview.status == "not_active":
            return rec.result("booking", "That booking is already cancelled.", flow={"agent": "booking", "action": None, "awaiting": None})
        if not preview.ok:
            return self._trouble(ctx, rec, preview)
        p = preview.payload
        return rec.result(
            "booking",
            f"If you cancel {s.reservation_id} now, you'll get {money(p['refund_amount'])} back. {p['explanation']} "
            "Do you want me to go ahead and cancel?",
            flow={**base, "awaiting": "confirm_cancel"},
        )

    async def _refund(self, ctx: AgentContext, flow: dict[str, Any]) -> AgentResult:
        rec, s = Recorder(), ctx.slots
        if (ask := await self._need_reference(ctx, rec, "refund")) is not None:
            return ask
        if not s.refund_amount:
            return rec.result(
                "booking", "I'm sorry to hear that. How much would you like refunded, and what happened?",
                flow={"agent": "booking", "action": "refund", "awaiting": "refund_details"},
            )
        outcome = await self._call(
            ctx, rec, "request_refund",
            reservation_id=s.reservation_id, guest_email=s.guest_email, amount=s.refund_amount, reason=ctx.user_text[:300],
        )
        if outcome.status == "approval_required":
            pending = make_pending(ctx, "request_refund", outcome.args, outcome.payload)
            return rec.result(
                "booking",
                f"I've passed your request for a {money(s.refund_amount)} refund on {s.reservation_id} to the duty manager "
                f"(ref {pending['approval_id']}). I'll update you here once it's been reviewed.",
                status="needs_approval",
                pending_action=pending,
                flow={"agent": "booking", "action": None, "awaiting": None},
            )
        if outcome.status == "not_found":
            return rec.result("booking", "I couldn't find a booking with that reference and email. Could you double-check both?",
                              flow={"agent": "booking", "action": "refund", "awaiting": "booking_ref"})
        if outcome.status == "invalid":
            return rec.result("booking", outcome.payload.get("message", "That amount isn't possible on this booking."),
                              flow={"agent": "booking", "action": "refund", "awaiting": "refund_details"})
        return self._trouble(ctx, rec, outcome)

    # ------------------------------------------------------------------------------------- approvals
    def after_approval(self, ctx: AgentContext) -> AgentResult:
        rec = Recorder()
        a = ctx.approval or {}
        label = ACTION_LABELS.get(a.get("action", ""), "request")
        idle = {"agent": "booking", "action": None, "awaiting": None}
        if not a.get("approved"):
            note = f" ({a['note']})" if a.get("note") else ""
            return rec.result(
                ctx.agent,
                f"Update on your {label}: the duty manager wasn't able to approve it{note}. Nothing has been changed. "
                "Is there anything else I can help with?",
                flow=idle,
            )
        outcome = a.get("outcome") or {}
        payload = outcome.get("payload") or {}
        rec.payloads.append(payload)
        if outcome.get("status") != "ok":
            return rec.result(
                ctx.agent,
                "Your request was approved, but I couldn't complete it in our system just now. I've passed it to the "
                "front desk, who will finish it and confirm with you here.",
                status="handoff",
                handoff_reason="approved_action_failed",
                flow=idle,
            )
        tool = a.get("tool")
        if tool == "create_booking":
            text = (
                f"Good news: your booking has been approved and confirmed. Your reference is {payload['reservation_id']} "
                f"({payload['room_name']}, {stay_text(payload['check_in'], payload['check_out'])}, total "
                f"{money(payload['total'])}). A confirmation and payment link are on their way to {payload['guest_email']}."
            )
            return rec.result(ctx.agent, text, flow=idle, slots_update={"reservation_id": payload["reservation_id"]})
        if tool == "cancel_booking":
            refund = (payload.get("cancellation") or {}).get("refund_amount", 0.0)
            text = (
                f"Good news: your cancellation was approved. Booking {payload['reservation_id']} is cancelled and "
                f"{money(refund)} will be refunded within 5 to 10 business days."
            )
            return rec.result(ctx.agent, text, flow=idle)
        if tool == "request_refund":
            text = (
                f"Good news: a refund of {money(payload.get('amount'))} on {payload['reservation']['reservation_id']} has "
                "been approved. It will reach your original payment method within 5 to 10 business days."
            )
            return rec.result(ctx.agent, text, flow=idle)
        if tool == "override_price":
            offer = _offer_from_quote(payload)
            text = (
                f"Good news: the duty manager approved a total of {money(payload['total'])} for the {payload['room_name']}, "
                f"{stay_text(payload['check_in'], payload['check_out'])}. Shall I book it at that price? Just reply with "
                "the lead guest's full name and email to confirm."
            )
            return rec.result(ctx.agent, text, flow={"agent": "booking", "action": "new", "awaiting": "confirm_booking", "offer": offer})
        return rec.result(ctx.agent, "Your request has been approved and completed.", flow=idle)

    # ------------------------------------------------------------------------------------- pricing
    async def pricing(self, ctx: AgentContext) -> AgentResult:
        if ctx.approval:
            return self.after_approval(ctx)
        rec, s = Recorder(), ctx.slots
        flow = dict(ctx.flow or {})
        offer = flow.get("offer")
        lowered = ctx.user_text.lower()
        target = ctx.decision.slots.target_total
        if target and offer and offer.get("quote_id"):
            outcome = await self._call(
                ctx, rec, "override_price", quote_id=offer["quote_id"], new_total=target, reason=ctx.user_text[:300]
            )
            if outcome.status == "approval_required":
                pending = make_pending(ctx, "override_price", outcome.args, outcome.payload)
                return rec.result(
                    "pricing",
                    f"I can't change prices myself, but I've asked the duty manager whether we can do "
                    f"{money(target)} instead of {money(offer['total'])} (ref {pending['approval_id']}). I'll let you know "
                    "here as soon as they reply.",
                    status="needs_approval",
                    pending_action=pending,
                    flow={**flow, "awaiting": None},
                )
            if not outcome.ok:
                return self._trouble(ctx, rec, outcome)
        explain = re.search(r"\b(why|how (is|are|do|does)|explain|breakdown|break down|what makes|so expensive|higher)\b", lowered)
        if explain and offer and not (ctx.decision.slots.check_in or ctx.decision.slots.room_type):
            parts = ", ".join(offer.get("adjustments") or []) or "no surcharges"
            disc = ", ".join(f"{d['label']} ({money(d['amount'])})" for d in offer.get("discounts", [])) or "none"
            return rec.result(
                "pricing",
                f"Your quote of {money(offer['total'])} is built from the base rate with these adjustments: {parts}. "
                f"Discounts: {disc}. Tax is {money(offer.get('tax'))}.",
                flow=flow,
            )
        if not s.stay_complete:
            if explain or re.search(r"\b(rates?|pricing|weekend|season|promo)\b", lowered):
                rules = await self._call(ctx, rec, "get_rate_rules")
                if not rules.ok:
                    return self._trouble(ctx, rec, rules)
                lines = rules.payload.get("summary", [])[:6]
                return rec.result("pricing", "Here's how our prices work:\n" + "\n".join(f"- {line}" for line in lines), flow=flow)
            return rec.result(
                "pricing",
                "I can get you an exact price. Which dates are you looking at, and how many guests?",
                flow={"agent": "booking", "action": "new", "awaiting": "stay_details", "offer": None},
            )
        if not s.room_type:
            avail = await self._call(
                ctx, rec, "search_availability",
                check_in=s.check_in, check_out=s.resolved_check_out, guests=s.guests, rooms=s.rooms or 1,
            )
            if not avail.ok:
                if avail.status == "invalid":
                    return rec.result("pricing", f"I couldn't price those dates: {avail.payload.get('message', '')}", flow=flow)
                return self._trouble(ctx, rec, avail)
            options = [o for o in avail.payload.get("options", []) if o.get("bookable")]
            if not options:
                return rec.result("pricing", "Sorry, nothing is available for those dates.", flow=flow)
            lines = [f"- {o['name']}: {money(o.get('indicative_total'))} total" for o in options]
            return rec.result(
                "pricing",
                f"Prices for {stay_text(s.check_in, s.resolved_check_out)}, {s.guests} guest{'s' if s.guests != 1 else ''} "
                "(including tax):\n" + "\n".join(lines) + "\nWould you like to book one of these?",
                flow={"agent": "booking", "action": "new", "awaiting": "room_choice", "offer": None},
            )
        quote = await self._call(
            ctx, rec, "get_quote",
            room_type=s.room_type, check_in=s.check_in, check_out=s.resolved_check_out, guests=s.guests,
            rooms=s.rooms or 1, promo_code=s.promo_code,
        )
        if not quote.ok:
            if quote.status == "invalid":
                return rec.result("pricing", quote.payload.get("message", "That combination isn't possible."), flow=flow)
            return self._trouble(ctx, rec, quote)
        offer = _offer_from_quote(quote.payload)
        return rec.result(
            "pricing",
            f"{_quote_text(quote.payload)} Would you like me to book it?",
            flow={"agent": "booking", "action": "new", "awaiting": "confirm_booking", "offer": offer},
        )

    # ------------------------------------------------------------------------------------- policy
    def extractive_answer(self, question: str, result: RetrievalResult) -> tuple[str, list[dict[str, Any]]]:
        """Quote the best-matching policy section (sections are written to stand alone), plus a close runner-up."""
        q_terms = set(tokenize(question))
        top = result.chunks[0]

        def sentences(text: str) -> list[str]:
            text = re.sub(r"^Q:\s.*?\?\s+A:\s*", "", text.strip())  # FAQ chunks: keep the answer only
            return [s for s in re.split(r"(?<=[.!?])\s+", text) if len(s) >= 12]

        parts = [" ".join(sentences(top.chunk.text)[:3]) + " [1]"]
        citations: list[dict[str, Any]] = [top.citation()]
        for hit in result.chunks[1:3]:
            if hit.score < 0.92 * top.score or hit.chunk.doc_id != top.chunk.doc_id:
                continue
            best = max(sentences(hit.chunk.text), key=lambda s: len(q_terms & set(tokenize(s))), default=None)
            if best and len(q_terms & set(tokenize(best))) >= 1:
                citations.append(hit.citation())
                parts.append(f"{best} [{len(citations)}]")
                break
        return " ".join(parts), citations

    async def policy(self, ctx: AgentContext, question: str) -> AgentResult:
        rec = Recorder()
        if self.retriever is None:
            return rec.result("policy", "I can't reach our policy library right now. The front desk can help: "
                              "frontdesk@aurora-bay.example.", status="handoff", handoff_reason="retrieval_unavailable")
        try:
            result = await self.retriever.search(question)
        except Exception:
            return rec.result("policy", "I can't reach our policy library right now. The front desk can help: "
                              "frontdesk@aurora-bay.example.", status="handoff", handoff_reason="retrieval_failure")
        retrieval = {"top_score": result.top_score, "chunks": [h.chunk.chunk_id for h in result.chunks]}
        if not result.found:
            return rec.result(
                "policy",
                "I couldn't find anything about that in our hotel policies, so I don't want to guess. I can connect you "
                "with the front desk if you'd like.",
                retrieval=retrieval,
            )
        text, citations = self.extractive_answer(question, result)
        rec.payloads.extend({"text": h.chunk.text} for h in result.chunks)
        return rec.result("policy", text, citations=citations, retrieval=retrieval)


def is_expired(offer: dict[str, Any] | None) -> bool:
    if not offer or not offer.get("expires_at"):
        return True
    from datetime import datetime

    return datetime.fromisoformat(offer["expires_at"]) < utcnow()
