"""Reservations service: bookings, changes, cancellations and refunds.

Risky operations are enforced here, at the system of record, not in the agent:
large bookings, cancellations with a big refund, and goodwill refunds return
403 `approval_required` until a signed operator approval token is supplied.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.approvals import ApprovalError, verify_token
from common.clock import now as clock_now
from common.config import get_settings
from common.domain import domain
from common.events import get_event_bus
from services.base import ServiceDB, as_utc, http_error, utc_now
from services.registry import service_client
from services.reservations.policy import cancellation_terms, modification_allowed

ALPHABET = "3456789ACDEFHJKMNPRTUVWXY"  # no look-alikes (0/O, 1/I/L, 2/Z, 5/S, 6/G, 8/B, Q) in references people read aloud
EmailStr = Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)]


class Base(DeclarativeBase):
    pass


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    guest_name: Mapped[str] = mapped_column(String(120))
    guest_email: Mapped[str] = mapped_column(String(200), index=True)
    guest_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    room_type: Mapped[str] = mapped_column(String(8))
    rooms: Mapped[int] = mapped_column(Integer, default=1)
    guests: Mapped[int] = mapped_column(Integer)
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    quote_id: Mapped[str] = mapped_column(String(32))
    total: Mapped[float] = mapped_column(Float)
    paid: Mapped[float] = mapped_column(Float)
    refunded: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    hold_id: Mapped[str] = mapped_column(String(32))
    nightly: Mapped[list] = mapped_column(JSON, default=list)
    special_requests: Mapped[str | None] = mapped_column(String(500), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    history: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    reservation_id: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(500))
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class UsedApproval(Base):
    __tablename__ = "used_approvals"
    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


db = ServiceDB(lambda: get_settings().reservations_db_url, Base.metadata)
Session = Annotated[AsyncSession, Depends(db.session)]


class CreateReservation(BaseModel):
    quote_id: str
    guest_name: str = Field(min_length=2, max_length=120)
    guest_email: EmailStr
    guest_phone: str | None = Field(default=None, max_length=40)
    special_requests: str | None = Field(default=None, max_length=500)
    channel: str | None = None
    approval_token: str | None = None


class ModifyReservation(BaseModel):
    guest_email: EmailStr
    check_in: date | None = None
    check_out: date | None = None
    room_type: str | None = None
    guests: int | None = Field(default=None, ge=1, le=40)
    rooms: int | None = Field(default=None, ge=1, le=20)
    promo_code: str | None = None


class CancelReservation(BaseModel):
    guest_email: EmailStr
    reason: str = Field(default="guest request", max_length=500)
    approval_token: str | None = None


class GoodwillRefund(BaseModel):
    guest_email: EmailStr
    amount: float = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)
    approval_token: str | None = None


app = FastAPI(title="Concierge Reservations Service", version="1.0.0")


def _new_id() -> str:
    return f"{domain().property.reservation_prefix}-" + "".join(secrets.choice(ALPHABET) for _ in range(6))


def serialize(res: Reservation) -> dict[str, Any]:
    room = domain().room(res.room_type)
    return {
        "reservation_id": res.id,
        "booking_status": res.status,
        "guest_name": res.guest_name,
        "guest_email": res.guest_email,
        "guest_phone": res.guest_phone,
        "room_type": res.room_type,
        "room_name": room.name if room else res.room_type,
        "rooms": res.rooms,
        "guests": res.guests,
        "check_in": res.check_in.isoformat(),
        "check_out": res.check_out.isoformat(),
        "nights": (res.check_out - res.check_in).days,
        "currency": res.currency,
        "total": round(res.total, 2),
        "paid": round(res.paid, 2),
        "refunded": round(res.refunded, 2),
        "quote_id": res.quote_id,
        "special_requests": res.special_requests,
        "channel": res.channel,
        "created_at": as_utc(res.created_at).isoformat(),
        "history": res.history,
    }


async def _owned(session: AsyncSession, reservation_id: str, email: str) -> Reservation:
    res = await session.get(Reservation, reservation_id.strip().upper())
    # Same 404 for "missing" and "wrong email": never confirm that a booking exists.
    if res is None or res.guest_email.lower() != email.strip().lower():
        raise http_error(404, "not_found", "No reservation matches that reference and email address.")
    return res


async def _fetch_quote(quote_id: str) -> dict:
    resp = await service_client("pricing").get(f"/quotes/{quote_id}")
    if resp.status_code == 404:
        raise http_error(404, "quote_not_found", f"quote {quote_id} not found; request a new quote")
    resp.raise_for_status()
    return resp.json()


async def _hold(room_type: str, check_in: str, check_out: str, rooms: int, reference: str) -> str:
    resp = await service_client("inventory").post(
        "/holds",
        json={"room_type": room_type, "check_in": check_in, "check_out": check_out, "rooms": rooms, "reference": reference},
    )
    if resp.status_code == 409:
        raise http_error(409, "unavailable", resp.json()["detail"]["message"])
    if resp.status_code == 422:
        raise http_error(422, "invalid_dates", resp.json()["detail"]["message"])
    resp.raise_for_status()
    return resp.json()["hold_id"]


async def _release(hold_id: str) -> None:
    resp = await service_client("inventory").post(f"/holds/{hold_id}/release")
    resp.raise_for_status()


async def _consume_approval(session: AsyncSession, action: str, bound: dict, token: str | None, reason: str, amount: float):
    try:
        claims = verify_token(token, action, bound)
    except ApprovalError as exc:
        raise http_error(
            403,
            "approval_required",
            f"This action needs manager approval ({exc}).",
            action=action,
            reason=reason,
            amount=round(amount, 2),
            bound_params=bound,
        ) from exc
    if await session.get(UsedApproval, claims.approval_id):
        raise http_error(409, "approval_already_used", "this approval token was already used")
    session.add(UsedApproval(approval_id=claims.approval_id))
    return claims


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "reservations"}


@app.post("/reservations", status_code=201)
async def create_reservation(
    session: Session, req: CreateReservation, idempotency_key: str | None = Header(default=None)
) -> dict:
    if idempotency_key:
        previous = await session.get(IdempotencyRecord, idempotency_key)
        if previous is not None:
            return previous.response
    quote = await _fetch_quote(req.quote_id.upper())
    if quote.get("expired"):
        raise http_error(409, "quote_expired", "That quote has expired; please request a fresh quote.")
    settings = get_settings()
    approval_id = None
    if quote["rooms"] >= settings.large_booking_rooms or quote["total"] >= settings.large_booking_total_threshold:
        bound = {"quote_id": quote["quote_id"], "guest_email": req.guest_email.lower()}
        claims = await _consume_approval(
            session,
            "create_large_booking",
            bound,
            req.approval_token,
            reason=(
                f"Large booking: {quote['rooms']} x {quote['room_name']} for {quote['nights']} nights, "
                f"total {quote['total']:.2f} {quote['currency']}"
            ),
            amount=quote["total"],
        )
        approval_id = claims.approval_id
    reservation_id = _new_id()
    hold_id = await _hold(quote["room_type"], quote["check_in"], quote["check_out"], quote["rooms"], reservation_id)
    res = Reservation(
        id=reservation_id,
        status="confirmed",
        guest_name=req.guest_name.strip(),
        guest_email=req.guest_email.lower(),
        guest_phone=req.guest_phone,
        room_type=quote["room_type"],
        rooms=quote["rooms"],
        guests=quote["guests"],
        check_in=date.fromisoformat(quote["check_in"]),
        check_out=date.fromisoformat(quote["check_out"]),
        quote_id=quote["quote_id"],
        total=quote["total"],
        paid=quote["total"],
        refunded=0.0,
        created_at=utc_now(),
        updated_at=utc_now(),
        currency=quote["currency"],
        hold_id=hold_id,
        nightly=quote["nightly"],
        special_requests=req.special_requests,
        channel=req.channel,
        history=[{"at": utc_now().isoformat(), "event": "created", "approval_id": approval_id}],
    )
    session.add(res)
    body = serialize(res)
    if idempotency_key:
        session.add(IdempotencyRecord(key=idempotency_key, response=body))
    await session.commit()
    await get_event_bus().publish("reservation.created", body, source="reservations")
    return body


@app.get("/reservations/{reservation_id}")
async def get_reservation(session: Session, reservation_id: str, email: str) -> dict:
    return serialize(await _owned(session, reservation_id, email))


@app.get("/reservations/{reservation_id}/cancellation-preview")
async def cancellation_preview(session: Session, reservation_id: str, email: str) -> dict:
    res = await _owned(session, reservation_id, email)
    if res.status != "confirmed":
        raise http_error(409, "not_active", f"reservation is {res.status}")
    terms = cancellation_terms(domain(), res.check_in, res.check_out, res.paid - res.refunded, res.nightly, clock_now())
    needs_approval = terms["refund_amount"] >= get_settings().cancel_refund_approval_threshold
    return {"reservation_id": res.id, **terms, "requires_approval": needs_approval}


@app.post("/reservations/{reservation_id}/cancel")
async def cancel_reservation(session: Session, reservation_id: str, req: CancelReservation) -> dict:
    res = await _owned(session, reservation_id, req.guest_email)
    if res.status != "confirmed":
        raise http_error(409, "not_active", f"reservation is already {res.status}")
    terms = cancellation_terms(domain(), res.check_in, res.check_out, res.paid - res.refunded, res.nightly, clock_now())
    approval_id = None
    if terms["refund_amount"] >= get_settings().cancel_refund_approval_threshold:
        bound = {"reservation_id": res.id, "refund_amount": terms["refund_amount"]}
        claims = await _consume_approval(
            session,
            "cancel_with_refund",
            bound,
            req.approval_token,
            reason=f"Cancellation of {res.id} refunds {terms['refund_amount']:.2f} {res.currency} ({terms['rule']})",
            amount=terms["refund_amount"],
        )
        approval_id = claims.approval_id
    await _release(res.hold_id)
    res.status = "cancelled"
    res.refunded = round(res.refunded + terms["refund_amount"], 2)
    res.updated_at = utc_now()
    res.history = [*res.history, {"at": utc_now().isoformat(), "event": "cancelled", "reason": req.reason, **terms}]
    if terms["refund_amount"] > 0:
        session.add(
            Refund(
                id="RF-" + secrets.token_hex(4).upper(),
                reservation_id=res.id,
                amount=terms["refund_amount"],
                kind="policy",
                reason=req.reason,
                approval_id=approval_id,
            )
        )
    await session.commit()
    body = {**serialize(res), "cancellation": terms}
    await get_event_bus().publish("reservation.cancelled", body, source="reservations")
    return body


@app.post("/reservations/{reservation_id}/modify")
async def modify_reservation(session: Session, reservation_id: str, req: ModifyReservation) -> dict:
    res = await _owned(session, reservation_id, req.guest_email)
    if res.status != "confirmed":
        raise http_error(409, "not_active", f"reservation is {res.status}")
    if not modification_allowed(domain(), res.check_in, clock_now()):
        raise http_error(
            409,
            "modification_window_closed",
            f"Changes are only possible up to {domain().policies.modification_cutoff_hours} hours before arrival; "
            "please talk to our front desk.",
        )
    check_in = req.check_in or res.check_in
    check_out = req.check_out or res.check_out
    if req.check_in and not req.check_out:
        check_out = check_in + (res.check_out - res.check_in)
    quote_resp = await service_client("pricing").post(
        "/quotes",
        json={
            "room_type": req.room_type or res.room_type,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "guests": req.guests or res.guests,
            "rooms": req.rooms or res.rooms,
            "promo_code": req.promo_code,
        },
    )
    if quote_resp.status_code >= 400:
        detail = quote_resp.json().get("detail", {})
        raise http_error(quote_resp.status_code, detail.get("error", "quote_failed"), detail.get("message", "quote failed"))
    quote = quote_resp.json()
    new_hold = await _hold(quote["room_type"], quote["check_in"], quote["check_out"], quote["rooms"], res.id)
    await _release(res.hold_id)
    old_total = res.total
    difference = round(quote["total"] - old_total, 2)
    res.hold_id = new_hold
    res.check_in, res.check_out = date.fromisoformat(quote["check_in"]), date.fromisoformat(quote["check_out"])
    res.room_type, res.rooms, res.guests = quote["room_type"], quote["rooms"], quote["guests"]
    res.quote_id, res.total, res.nightly = quote["quote_id"], quote["total"], quote["nightly"]
    if difference > 0:
        res.paid = round(res.paid + difference, 2)
    elif difference < 0:
        res.refunded = round(res.refunded - difference, 2)
        session.add(
            Refund(
                id="RF-" + secrets.token_hex(4).upper(),
                reservation_id=res.id,
                amount=-difference,
                kind="modification",
                reason="price difference after change",
            )
        )
    res.updated_at = utc_now()
    res.history = [
        *res.history,
        {"at": utc_now().isoformat(), "event": "modified", "old_total": old_total, "new_total": quote["total"]},
    ]
    await session.commit()
    body = {
        **serialize(res),
        "price_difference": difference,
        "settlement": "charged" if difference > 0 else ("refunded" if difference < 0 else "none"),
    }
    await get_event_bus().publish("reservation.modified", body, source="reservations")
    return body


@app.post("/reservations/{reservation_id}/refunds", status_code=201)
async def goodwill_refund(session: Session, reservation_id: str, req: GoodwillRefund) -> dict:
    res = await _owned(session, reservation_id, req.guest_email)
    refundable = round(res.paid - res.refunded, 2)
    if req.amount > refundable:
        raise http_error(422, "amount_too_high", f"at most {refundable:.2f} can be refunded on this booking")
    bound = {"reservation_id": res.id, "amount": round(req.amount, 2)}
    claims = await _consume_approval(
        session,
        "goodwill_refund",
        bound,
        req.approval_token,
        reason=f"Goodwill refund of {req.amount:.2f} {res.currency} on {res.id}: {req.reason}",
        amount=req.amount,
    )
    refund = Refund(
        id="RF-" + secrets.token_hex(4).upper(),
        reservation_id=res.id,
        amount=round(req.amount, 2),
        kind="goodwill",
        reason=req.reason,
        approval_id=claims.approval_id,
    )
    session.add(refund)
    res.refunded = round(res.refunded + req.amount, 2)
    res.history = [*res.history, {"at": utc_now().isoformat(), "event": "goodwill_refund", "amount": req.amount}]
    await session.commit()
    body = {"refund_id": refund.id, "amount": refund.amount, "reservation": serialize(res)}
    await get_event_bus().publish("refund.issued", body, source="reservations")
    return body


@app.get("/reservations")
async def list_reservations(session: Session, email: str) -> list[dict]:
    rows = (
        await session.execute(select(Reservation).where(Reservation.guest_email == email.strip().lower()))
    ).scalars()
    return [serialize(r) for r in rows]
