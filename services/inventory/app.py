"""Inventory service: room types, nightly allotments and atomic holds."""

from __future__ import annotations

import hashlib
import math
import secrets
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import Date, DateTime, Integer, String, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.clock import nights_between, today
from common.config import get_settings
from common.domain import RoomType, domain
from services.base import ServiceDB, http_error, insert_ignore, utc_now


class Base(DeclarativeBase):
    pass


class InventoryDay(Base):
    __tablename__ = "inventory_days"
    room_type: Mapped[str] = mapped_column(String(8), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    total: Mapped[int] = mapped_column(Integer)
    booked: Mapped[int] = mapped_column(Integer)


class Hold(Base):
    __tablename__ = "holds"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    room_type: Mapped[str] = mapped_column(String(8))
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    rooms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


db = ServiceDB(lambda: get_settings().inventory_db_url, Base.metadata)
Session = Annotated[AsyncSession, Depends(db.session)]


def background_booked(room: RoomType, day: date) -> int:
    """Deterministic 'other guests' occupancy so dynamic pricing has something to react to."""
    rules = domain().rate_rules
    noise = int(hashlib.sha256(f"{room.code}:{day.isoformat()}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    occ = 0.28 + 0.15 * noise
    if day.weekday() in (4, 5):
        occ += 0.18
    if any(s.pct > 0 and s.contains(day) for s in rules.seasons):
        occ += 0.30
    if any(s.pct < 0 and s.contains(day) for s in rules.seasons):
        occ -= 0.15
    occ = max(0.0, min(occ, 0.95))
    if room.inventory == 1:
        return 1 if noise < occ * 0.5 else 0
    # always leave at least two rooms of each multi-room type for live bookings
    return min(room.inventory - 2, math.floor(room.inventory * occ))


async def ensure_days(session: AsyncSession, room: RoomType, start: date, end: date) -> None:
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    if not days:
        return
    existing = set(
        (
            await session.execute(
                select(InventoryDay.day).where(
                    InventoryDay.room_type == room.code, InventoryDay.day >= start, InventoryDay.day < end
                )
            )
        ).scalars()
    )
    missing = [
        {"room_type": room.code, "day": d, "total": room.inventory, "booked": background_booked(room, d)}
        for d in days
        if d not in existing
    ]
    if missing:
        await session.execute(insert_ignore(session, InventoryDay.__table__, missing))


def validate_stay(check_in: date, check_out: date) -> None:
    if check_out <= check_in:
        raise http_error(422, "invalid_dates", "check_out must be after check_in")
    if check_in < today():
        raise http_error(422, "invalid_dates", f"check_in {check_in} is in the past (today is {today()})")
    if (check_out - check_in).days > domain().policies.max_nights:
        raise http_error(422, "invalid_dates", f"stays are limited to {domain().policies.max_nights} nights")


def room_or_404(code: str) -> RoomType:
    room = domain().room(code)
    if room is None:
        valid = ", ".join(r.code for r in domain().room_types)
        raise http_error(404, "unknown_room_type", f"unknown room type {code!r}; valid codes: {valid}")
    return room


class HoldRequest(BaseModel):
    room_type: str
    check_in: date
    check_out: date
    rooms: int = Field(default=1, ge=1, le=20)
    reference: str | None = None


app = FastAPI(title="Concierge Inventory Service", version="1.0.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "inventory"}


@app.get("/room-types")
async def room_types() -> list[dict]:
    return [r.model_dump(exclude={"aliases"}) for r in domain().room_types]


@app.get("/availability")
async def availability(
    session: Session,
    check_in: date,
    check_out: date,
    guests: int = Query(default=1, ge=1, le=40),
    rooms: int = Query(default=1, ge=1, le=20),
    room_type: str | None = None,
) -> dict:
    validate_stay(check_in, check_out)
    candidates = [room_or_404(room_type)] if room_type else domain().room_types
    options = []
    for room in candidates:
        await ensure_days(session, room, check_in, check_out)
        rows = (
            await session.execute(
                select(InventoryDay).where(
                    InventoryDay.room_type == room.code, InventoryDay.day >= check_in, InventoryDay.day < check_out
                )
            )
        ).scalars()
        free = min((r.total - r.booked for r in rows), default=0)
        fits = guests <= room.max_guests * rooms
        options.append(
            {
                "room_type": room.code,
                "name": room.name,
                "max_guests": room.max_guests,
                "available_rooms": max(free, 0),
                "fits_party": fits,
                "bookable": fits and free >= rooms,
            }
        )
    await session.commit()
    return {
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "nights": (check_out - check_in).days,
        "guests": guests,
        "rooms": rooms,
        "options": options,
    }


@app.get("/occupancy")
async def occupancy(session: Session, room_type: str, check_in: date, check_out: date) -> dict:
    room = room_or_404(room_type)
    if check_out <= check_in:
        raise http_error(422, "invalid_dates", "check_out must be after check_in")
    await ensure_days(session, room, check_in, check_out)
    await session.commit()
    rows = (
        await session.execute(
            select(InventoryDay)
            .where(InventoryDay.room_type == room.code, InventoryDay.day >= check_in, InventoryDay.day < check_out)
            .order_by(InventoryDay.day)
        )
    ).scalars()
    return {
        "room_type": room.code,
        "nights": [
            {"date": r.day.isoformat(), "total": r.total, "booked": r.booked, "occupancy": round(r.booked / r.total, 3)}
            for r in rows
        ],
    }


@app.post("/holds", status_code=201)
async def create_hold(session: Session, req: HoldRequest) -> dict:
    room = room_or_404(req.room_type)
    validate_stay(req.check_in, req.check_out)
    hold_id = "H-" + secrets.token_hex(6).upper()
    await ensure_days(session, room, req.check_in, req.check_out)
    for night in nights_between(req.check_in, req.check_out):
        result = await session.execute(
            update(InventoryDay)
            .where(
                InventoryDay.room_type == room.code,
                InventoryDay.day == night,
                InventoryDay.booked + req.rooms <= InventoryDay.total,
            )
            .values(booked=InventoryDay.booked + req.rooms)
        )
        if result.rowcount != 1:
            await session.rollback()
            raise http_error(
                409, "unavailable", f"{room.name} is sold out on {night.isoformat()}", night=night.isoformat()
            )
    session.add(
        Hold(
            id=hold_id,
            room_type=room.code,
            check_in=req.check_in,
            check_out=req.check_out,
            rooms=req.rooms,
            reference=req.reference,
        )
    )
    await session.commit()
    return {"hold_id": hold_id, "room_type": room.code, "rooms": req.rooms, "status": "active"}


@app.get("/holds/{hold_id}")
async def get_hold(session: Session, hold_id: str) -> dict:
    hold = await session.get(Hold, hold_id)
    if hold is None:
        raise http_error(404, "not_found", f"hold {hold_id} not found")
    return {
        "hold_id": hold.id,
        "room_type": hold.room_type,
        "check_in": hold.check_in.isoformat(),
        "check_out": hold.check_out.isoformat(),
        "rooms": hold.rooms,
        "status": hold.status,
    }


@app.post("/holds/{hold_id}/release")
async def release_hold(session: Session, hold_id: str) -> dict:
    hold = await session.get(Hold, hold_id)
    if hold is None:
        raise http_error(404, "not_found", f"hold {hold_id} not found")
    if hold.status == "released":
        return {"hold_id": hold_id, "status": "released"}
    for night in nights_between(hold.check_in, hold.check_out):
        await session.execute(
            update(InventoryDay)
            .where(InventoryDay.room_type == hold.room_type, InventoryDay.day == night)
            .values(booked=InventoryDay.booked - hold.rooms)
        )
    hold.status = "released"
    await session.commit()
    return {"hold_id": hold_id, "status": "released"}
