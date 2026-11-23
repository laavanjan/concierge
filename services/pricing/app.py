"""Pricing service: dynamic quotes (calls inventory for live occupancy) and approved overrides."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, Float, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.approvals import ApprovalError, verify_token
from common.config import get_settings
from common.domain import domain
from services.base import ServiceDB, as_utc, http_error, utc_now
from services.pricing.engine import price_stay, rules_summary
from services.registry import service_client


class Base(DeclarativeBase):
    pass


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    room_type: Mapped[str] = mapped_column(String(8))
    total: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsedApproval(Base):
    __tablename__ = "used_approvals"
    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


db = ServiceDB(lambda: get_settings().pricing_db_url, Base.metadata)
Session = Annotated[AsyncSession, Depends(db.session)]


class QuoteRequest(BaseModel):
    room_type: str
    check_in: date
    check_out: date
    guests: int = Field(ge=1, le=40)
    rooms: int = Field(default=1, ge=1, le=20)
    promo_code: str | None = None
    persist: bool = True


class OverrideRequest(BaseModel):
    new_total: float = Field(gt=0)
    reason: str = Field(min_length=3, max_length=500)
    approval_token: str | None = None


app = FastAPI(title="Concierge Pricing Service", version="1.0.0")


async def _occupancy(room_type: str, check_in: date, check_out: date) -> dict[str, float]:
    client = service_client("inventory")
    resp = await client.get(
        "/occupancy",
        params={"room_type": room_type, "check_in": check_in.isoformat(), "check_out": check_out.isoformat()},
    )
    if resp.status_code == 404:
        raise http_error(404, "unknown_room_type", resp.json()["detail"]["message"])
    resp.raise_for_status()
    return {n["date"]: n["occupancy"] for n in resp.json()["nights"]}


def _serialize(quote: Quote) -> dict[str, Any]:
    return {
        "quote_id": quote.id,
        **quote.payload,
        "created_at": as_utc(quote.created_at).isoformat(),
        "expires_at": as_utc(quote.expires_at).isoformat(),
        "expired": as_utc(quote.expires_at) < utc_now(),
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "pricing"}


@app.get("/rate-rules")
async def rate_rules() -> dict:
    pack = domain()
    return {"currency": pack.property.currency, "summary": rules_summary(pack), "rules": pack.rate_rules.model_dump(mode="json")}


@app.post("/quotes", status_code=201)
async def create_quote(session: Session, req: QuoteRequest) -> dict:
    pack = domain()
    room = pack.room(req.room_type)
    if room is None:
        raise http_error(404, "unknown_room_type", f"unknown room type {req.room_type!r}")
    if req.check_out <= req.check_in:
        raise http_error(422, "invalid_dates", "check_out must be after check_in")
    if req.rooms > pack.policies.max_rooms_per_booking:
        raise http_error(422, "too_many_rooms", f"max {pack.policies.max_rooms_per_booking} rooms per booking")
    if req.guests > room.max_guests * req.rooms:
        raise http_error(
            422,
            "over_capacity",
            f"{room.name} sleeps {room.max_guests}; {req.guests} guests need at least "
            f"{-(-req.guests // room.max_guests)} rooms",
        )
    occupancy = await _occupancy(room.code, req.check_in, req.check_out)
    payload = price_stay(pack, room, req.check_in, req.check_out, req.guests, req.rooms, occupancy, req.promo_code)
    if not req.persist:
        return {"quote_id": None, **payload}
    now = utc_now()
    quote = Quote(
        id="Q-" + secrets.token_hex(4).upper(),
        room_type=room.code,
        total=payload["total"],
        payload=payload,
        created_at=now,
        expires_at=now + timedelta(minutes=pack.rate_rules.quote_ttl_minutes),
    )
    session.add(quote)
    await session.commit()
    return _serialize(quote)


@app.get("/quotes/{quote_id}")
async def get_quote(session: Session, quote_id: str) -> dict:
    quote = await session.get(Quote, quote_id.upper())
    if quote is None:
        raise http_error(404, "not_found", f"quote {quote_id} not found")
    return _serialize(quote)


@app.post("/quotes/{quote_id}/override", status_code=201)
async def override_quote(session: Session, quote_id: str, req: OverrideRequest) -> dict:
    """Price override: always requires a signed operator approval bound to (quote, new total)."""
    quote = await session.get(Quote, quote_id.upper())
    if quote is None:
        raise http_error(404, "not_found", f"quote {quote_id} not found")
    bound = {"quote_id": quote.id, "new_total": round(req.new_total, 2)}
    try:
        claims = verify_token(req.approval_token, "override_price", bound)
    except ApprovalError as exc:
        raise http_error(
            403,
            "approval_required",
            f"Price overrides need manager approval ({exc}).",
            action="override_price",
            reason=f"Override quote {quote.id} from {quote.total:.2f} to {req.new_total:.2f}: {req.reason}",
            amount=round(quote.total - req.new_total, 2),
            bound_params=bound,
        ) from exc
    if await session.get(UsedApproval, claims.approval_id):
        raise http_error(409, "approval_already_used", "this approval was already used")
    session.add(UsedApproval(approval_id=claims.approval_id))
    payload = dict(quote.payload)
    payload["override"] = {
        "previous_total": quote.total,
        "new_total": round(req.new_total, 2),
        "reason": req.reason,
        "approved_by": claims.approved_by,
        "approval_id": claims.approval_id,
    }
    payload["total"] = round(req.new_total, 2)
    now = utc_now()
    new_quote = Quote(
        id="Q-" + secrets.token_hex(4).upper(),
        room_type=quote.room_type,
        total=payload["total"],
        payload=payload,
        created_at=now,
        expires_at=now + timedelta(minutes=domain().rate_rules.quote_ttl_minutes),
    )
    session.add(new_quote)
    await session.commit()
    return _serialize(new_quote)
