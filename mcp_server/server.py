"""Concierge backend tools over the Model Context Protocol.

Agents never call the microservices directly: every backend action goes through
these tools, so the same tool surface also works from Claude Desktop, Claude
Code or any other MCP client (`python -m mcp_server --transport stdio`).

Parameters named `approval_token`, `idempotency_key` and `channel` are set by
the agent runtime, never by the model: the runtime strips them from the tool
schema it shows the LLM and injects them itself.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from common.config import get_settings
from common.domain import domain
from services.client import BackendClient, BackendUnavailable

RUNTIME_ONLY_PARAMS = frozenset({"approval_token", "idempotency_key", "channel"})

server = MCPServer(
    name="concierge-backend",
    title="Concierge reservation backend",
    version="1.0.0",
    instructions=(
        "Tools for searching availability, quoting, booking, changing and cancelling stays at the property. "
        "Quote before you book; bookings, cancellations and refunds may return status=approval_required, "
        "which means a human manager must approve before the action happens."
    ),
)
backend = BackendClient()

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


async def _guard(coro: Any) -> dict[str, Any]:
    try:
        result = await coro
    except BackendUnavailable as exc:
        raise ToolError(f"backend_unavailable: {exc}") from exc
    return result.to_tool_payload()


@server.tool(annotations=READ_ONLY)
async def list_room_types() -> dict[str, Any]:
    """List the room types (code, name, max guests, beds, size, view, pets) with descriptions.

    Has no prices or availability; use search_availability or get_quote for those.
    """
    return await _guard(backend.room_types())


@server.tool(annotations=READ_ONLY)
async def search_availability(
    check_in: date, check_out: date, guests: int, rooms: int = 1, room_type: str | None = None
) -> dict[str, Any]:
    """Find which room types are free for a stay, each with an indicative total price (incl. tax).

    Args:
        check_in: Arrival date, YYYY-MM-DD.
        check_out: Departure date, YYYY-MM-DD (must be after check_in).
        guests: Total number of guests, adults plus children.
        rooms: Number of rooms wanted (default 1).
        room_type: Optional room code to check a single type: STD, DLX, FAM or PEN.
    """
    payload = await _guard(
        backend.availability(check_in.isoformat(), check_out.isoformat(), guests, rooms, room_type)
    )
    if payload.get("status") != "ok":
        return payload
    for option in payload.get("options", []):
        if not option.get("bookable"):
            continue
        preview = await _guard(
            backend.quote(
                room_type=option["room_type"],
                check_in=check_in.isoformat(),
                check_out=check_out.isoformat(),
                guests=guests,
                rooms=rooms,
                persist=False,
            )
        )
        if preview.get("status") == "ok":
            option["indicative_total"] = preview["total"]
            option["currency"] = preview["currency"]
    return payload


@server.tool(annotations=WRITE)
async def get_quote(
    room_type: str,
    check_in: date,
    check_out: date,
    guests: int,
    rooms: int = 1,
    promo_code: str | None = None,
) -> dict[str, Any]:
    """Get a bookable price quote with a nightly breakdown, discounts, tax and a quote_id (valid 30 minutes).

    Always quote before booking and tell the guest the total from this result.

    Args:
        room_type: Room code: STD, DLX, FAM or PEN.
        check_in: Arrival date, YYYY-MM-DD.
        check_out: Departure date, YYYY-MM-DD.
        guests: Total number of guests.
        rooms: Number of rooms (default 1).
        promo_code: Optional promotion code the guest gave you.
    """
    return await _guard(
        backend.quote(
            room_type=room_type.upper(),
            check_in=check_in.isoformat(),
            check_out=check_out.isoformat(),
            guests=guests,
            rooms=rooms,
            promo_code=promo_code,
        )
    )


@server.tool(annotations=WRITE)
async def create_booking(
    quote_id: str,
    guest_name: str,
    guest_email: str,
    guest_phone: str | None = None,
    special_requests: str | None = None,
    approval_token: str | None = None,
    idempotency_key: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Book the stay described by a quote. Only call after the guest explicitly confirmed the quoted total.

    Large bookings return status=approval_required and wait for a manager.

    Args:
        quote_id: quote_id returned by get_quote.
        guest_name: Lead guest's full name.
        guest_email: Lead guest's email address (used to manage the booking later).
        guest_phone: Optional phone number.
        special_requests: Optional requests (late arrival, high floor, cot...).
    """
    body = {
        "quote_id": quote_id,
        "guest_name": guest_name,
        "guest_email": guest_email,
        "guest_phone": guest_phone,
        "special_requests": special_requests,
        "channel": channel,
        "approval_token": approval_token,
    }
    return await _guard(backend.create_reservation(body, idempotency_key))


@server.tool(annotations=READ_ONLY)
async def get_booking(reservation_id: str, guest_email: str) -> dict[str, Any]:
    """Look up a reservation. Needs both the reference (e.g. AUR-7K3Q9P) and the email used to book.

    Args:
        reservation_id: Booking reference such as AUR-7K3Q9P.
        guest_email: Email address on the booking.
    """
    return await _guard(backend.get_reservation(reservation_id, guest_email))


@server.tool(annotations=WRITE)
async def modify_booking(
    reservation_id: str,
    guest_email: str,
    check_in: date | None = None,
    check_out: date | None = None,
    room_type: str | None = None,
    guests: int | None = None,
    rooms: int | None = None,
) -> dict[str, Any]:
    """Change dates, room type or party size of a booking; re-prices it and returns price_difference.

    Only pass the fields that change. Allowed up to 48 hours before arrival.

    Args:
        reservation_id: Booking reference.
        guest_email: Email address on the booking.
        check_in: New arrival date, YYYY-MM-DD.
        check_out: New departure date, YYYY-MM-DD.
        room_type: New room code.
        guests: New number of guests.
        rooms: New number of rooms.
    """
    body = {
        "guest_email": guest_email,
        "check_in": check_in.isoformat() if check_in else None,
        "check_out": check_out.isoformat() if check_out else None,
        "room_type": room_type.upper() if room_type else None,
        "guests": guests,
        "rooms": rooms,
    }
    return await _guard(backend.modify(reservation_id, body))


@server.tool(annotations=READ_ONLY)
async def preview_cancellation(reservation_id: str, guest_email: str) -> dict[str, Any]:
    """Show what cancelling would refund under the cancellation policy, without cancelling.

    Use this and tell the guest the refund before calling cancel_booking.

    Args:
        reservation_id: Booking reference.
        guest_email: Email address on the booking.
    """
    return await _guard(backend.cancellation_preview(reservation_id, guest_email))


@server.tool(annotations=DESTRUCTIVE)
async def cancel_booking(
    reservation_id: str, guest_email: str, reason: str = "guest request", approval_token: str | None = None
) -> dict[str, Any]:
    """Cancel a booking and refund per policy. Only after the guest confirmed they want to cancel.

    Refunds above the auto-approval limit return status=approval_required.

    Args:
        reservation_id: Booking reference.
        guest_email: Email address on the booking.
        reason: Short reason given by the guest.
    """
    return await _guard(backend.cancel(reservation_id, guest_email, reason, approval_token))


@server.tool(annotations=DESTRUCTIVE)
async def request_refund(
    reservation_id: str, guest_email: str, amount: float, reason: str, approval_token: str | None = None
) -> dict[str, Any]:
    """Request a goodwill refund outside the cancellation policy (service recovery). Always needs a manager.

    Args:
        reservation_id: Booking reference.
        guest_email: Email address on the booking.
        amount: Refund amount in the booking currency.
        reason: Why the guest should be refunded.
    """
    return await _guard(backend.goodwill_refund(reservation_id, guest_email, amount, reason, approval_token))


@server.tool(annotations=READ_ONLY)
async def get_rate_rules() -> dict[str, Any]:
    """Explain how prices are built: base rates, weekend/season/demand adjustments, stay discounts, promos, tax."""
    return await _guard(backend.rate_rules())


@server.tool(annotations=WRITE)
async def override_price(
    quote_id: str, new_total: float, reason: str, approval_token: str | None = None
) -> dict[str, Any]:
    """Ask a manager to override a quote's total (price match, service recovery). Always needs approval.

    Args:
        quote_id: The quote to override.
        new_total: Proposed new total including tax.
        reason: Business reason for the override.
    """
    return await _guard(backend.override_price(quote_id, new_total, reason, approval_token))


# ---- resources: the policy library, so MCP clients can read the same documents ----
@server.resource("policy://index", mime_type="application/json", description="List of policy documents")
def policy_index() -> str:
    docs = sorted(p.name for p in get_settings().policies_dir.glob("*") if p.is_file())
    return json.dumps({"property": domain().property.name, "documents": docs})


@server.resource("policy://docs/{name}", mime_type="text/markdown", description="A policy document by file name")
def policy_document(name: str) -> str:
    allowed = {p.name: p for p in get_settings().policies_dir.glob("*.md")}
    if name not in allowed:
        raise ToolError(f"unknown policy document {name!r}")
    return allowed[name].read_text(encoding="utf-8")
