"""Event-driven notifications: react to backend domain events (Redis Streams / in-process bus)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from channels.email_channel import send_email
from common.domain import domain

log = logging.getLogger(__name__)


async def handle_event(event: dict[str, Any]) -> None:
    kind, payload = event.get("type"), event.get("payload", {})
    name = domain().property.name
    if kind == "reservation.created":
        body = (
            f"Hi {payload.get('guest_name', '').split(' ')[0] or 'there'},\n\nYour booking {payload['reservation_id']} at "
            f"{name} is confirmed: {payload['room_name']}, {payload['check_in']} to {payload['check_out']}, "
            f"{payload['guests']} guest(s), total {payload['total']:.2f} {payload['currency']}.\n\n"
            "A secure payment link follows in a separate email (demo: not sent).\n"
        )
        await asyncio.to_thread(send_email, payload["guest_email"], f"Booking confirmed: {payload['reservation_id']}", body)
    elif kind == "reservation.cancelled":
        refund = (payload.get("cancellation") or {}).get("refund_amount", 0.0)
        body = f"Your booking {payload['reservation_id']} has been cancelled. Refund: {refund:.2f} {payload.get('currency', 'USD')}.\n"
        await asyncio.to_thread(send_email, payload["guest_email"], f"Booking cancelled: {payload['reservation_id']}", body)
    elif kind == "refund.issued":
        res = payload.get("reservation", {})
        body = f"A refund of {payload.get('amount', 0):.2f} on booking {res.get('reservation_id')} has been issued.\n"
        if res.get("guest_email"):
            await asyncio.to_thread(send_email, res["guest_email"], "Refund issued", body)
    else:
        log.debug("ignoring event %s", kind)
