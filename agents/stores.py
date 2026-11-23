"""Persistence for the agent layer.

* `CustomerMemory`: long-term customer memory (identities across channels, stated preferences). Postgres in
  production, SQLite in dev.
* `HitlStore`: approval requests and operator handoff tickets.
* `SessionStore`: short-term session state and the outbound message box (Redis in production, in-memory in dev).
  The conversation state itself lives in the LangGraph checkpointer, keyed by session id.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from common.config import get_settings
from services.base import ServiceDB, as_utc, utc_now


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Identity(Base):
    __tablename__ = "identities"
    channel: Mapped[str] = mapped_column(String(16), primary_key=True)
    address: Mapped[str] = mapped_column(String(200), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(24), index=True)


class Fact(Base):
    __tablename__ = "customer_facts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(String(24), index=True)
    text: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(120), index=True)
    customer_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    channel: Mapped[str] = mapped_column(String(16))
    agent: Mapped[str] = mapped_column(String(16))
    tool: Mapped[str] = mapped_column(String(40))
    args: Mapped[dict] = mapped_column(JSON)
    action: Mapped[str] = mapped_column(String(40))
    bound_params: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Handoff(Base):
    __tablename__ = "handoffs"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(120), index=True)
    customer_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    channel: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    claimed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _row(obj: Any) -> dict[str, Any]:
    out = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        out[col.name] = as_utc(value).isoformat() if isinstance(value, datetime) else value
    return out


_db = ServiceDB(lambda: get_settings().memory_db_url, Base.metadata)

PREFERENCE_PATTERNS = [
    r"\b(i|we) (always |usually |really )?(prefer|like|love|need|want) (a |an )?(high|low|quiet|top|ground)[^.!?]{0,60}",
    r"\b(i'?m|i am|we'?re|we are) (allergic|vegan|vegetarian|gluten[- ]free|coeliac|celiac|lactose)[^.!?]{0,60}",
    r"\b(i|we)('m| am|'re| are)? (travel+ing|coming) with (my |our |a )?(dog|baby|kids|children|wheelchair)[^.!?]{0,40}",
    r"\b(please )?remember (that )?[^.!?]{3,100}",
    r"\b(i|we) (always|usually) (arrive|check in|come) (late|early)[^.!?]{0,40}",
    r"\b(i|we) (use|need) a wheelchair[^.!?]{0,40}",
]


def extract_preferences(text: str) -> list[str]:
    facts = []
    for pattern in PREFERENCE_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            fact = match.group(0).strip(" ,;")
            if 8 <= len(fact) <= 160:
                facts.append(fact[0].upper() + fact[1:])
    return facts[:3]


class CustomerMemory:
    async def _maker(self):
        return await _db.ensure()

    async def resolve(self, channel: str, address: str) -> str:
        maker = await self._maker()
        async with maker() as session:
            ident = await session.get(Identity, (channel, address.lower()))
            if ident:
                return ident.customer_id
            customer = Customer(id="C-" + secrets.token_hex(5).upper(), created_at=utc_now())
            if channel == "email":
                customer.email = address.lower()
            elif channel in ("whatsapp", "voice"):
                customer.phone = address
            session.add(customer)
            session.add(Identity(channel=channel, address=address.lower(), customer_id=customer.id))
            await session.commit()
            return customer.id

    async def link_contact(self, customer_id: str, email: str | None = None, phone: str | None = None, name: str | None = None) -> str:
        """Attach contact details; if the email already belongs to another profile, merge into it."""
        maker = await self._maker()
        async with maker() as session:
            target = customer_id
            if email:
                existing = (await session.execute(select(Customer).where(Customer.email == email.lower()))).scalars().first()
                if existing and existing.id != customer_id:
                    await session.execute(update(Identity).where(Identity.customer_id == customer_id).values(customer_id=existing.id))
                    await session.execute(update(Fact).where(Fact.customer_id == customer_id).values(customer_id=existing.id))
                    target = existing.id
            customer = await session.get(Customer, target)
            if customer is None:
                customer = Customer(id=target, created_at=utc_now())
                session.add(customer)
            if email and not customer.email:
                customer.email = email.lower()
            if phone and not customer.phone:
                customer.phone = phone
            if name and not customer.name:
                customer.name = name
            if email and await session.get(Identity, ("email", email.lower())) is None:
                session.add(Identity(channel="email", address=email.lower(), customer_id=target))
            await session.commit()
            return target

    async def remember(self, customer_id: str, facts: list[str], source: str) -> None:
        if not facts:
            return
        maker = await self._maker()
        async with maker() as session:
            known = set(
                (await session.execute(select(Fact.text).where(Fact.customer_id == customer_id))).scalars()
            )
            for fact in facts:
                if fact not in known:
                    session.add(Fact(customer_id=customer_id, text=fact, source=source, created_at=utc_now()))
            await session.commit()

    async def profile(self, customer_id: str | None) -> dict[str, Any]:
        if not customer_id:
            return {}
        maker = await self._maker()
        async with maker() as session:
            customer = await session.get(Customer, customer_id)
            if customer is None:
                return {"customer_id": customer_id}
            facts = (
                await session.execute(
                    select(Fact.text).where(Fact.customer_id == customer_id).order_by(Fact.id.desc()).limit(8)
                )
            ).scalars()
            return {
                "customer_id": customer.id,
                "name": customer.name,
                "email": customer.email,
                "phone": customer.phone,
                "preferences": list(facts),
            }


class HitlStore:
    async def _maker(self):
        return await _db.ensure()

    async def create_approval(self, pending: dict[str, Any], session_id: str, channel: str, customer_id: str | None) -> dict:
        maker = await self._maker()
        async with maker() as session:
            existing = await session.get(Approval, pending["approval_id"])
            if existing:
                return _row(existing)
            approval = Approval(
                id=pending["approval_id"],
                session_id=session_id,
                customer_id=customer_id,
                channel=channel,
                agent=pending["agent"],
                tool=pending["tool"],
                args=pending["args"],
                action=pending["action"],
                bound_params=pending["bound_params"],
                reason=pending.get("reason", ""),
                amount=float(pending.get("amount") or 0.0),
                created_at=utc_now(),
            )
            session.add(approval)
            await session.commit()
            return _row(approval)

    async def get_approval(self, approval_id: str) -> dict | None:
        maker = await self._maker()
        async with maker() as session:
            row = await session.get(Approval, approval_id)
            return _row(row) if row else None

    async def list_approvals(self, status: str | None = "pending") -> list[dict]:
        maker = await self._maker()
        async with maker() as session:
            query = select(Approval).order_by(Approval.created_at.desc()).limit(200)
            if status:
                query = query.where(Approval.status == status)
            return [_row(r) for r in (await session.execute(query)).scalars()]

    async def decide(self, approval_id: str, status: str, operator: str, note: str | None) -> dict | None:
        maker = await self._maker()
        async with maker() as session:
            row = await session.get(Approval, approval_id)
            if row is None:
                return None
            row.status, row.decided_by, row.note, row.decided_at = status, operator, note, utc_now()
            await session.commit()
            return _row(row)

    async def set_outcome(self, approval_id: str, outcome: dict[str, Any], status: str | None = None) -> None:
        maker = await self._maker()
        async with maker() as session:
            row = await session.get(Approval, approval_id)
            if row is not None:
                row.outcome = json.loads(json.dumps(outcome, default=str))
                if status:
                    row.status = status
                await session.commit()

    async def create_handoff(self, session_id: str, channel: str, customer_id: str | None, reason: str, summary: str) -> dict:
        maker = await self._maker()
        async with maker() as session:
            open_ticket = (
                await session.execute(
                    select(Handoff).where(Handoff.session_id == session_id, Handoff.status.in_(["open", "claimed"]))
                )
            ).scalars().first()
            if open_ticket:
                return _row(open_ticket)
            ticket = Handoff(
                id="HO-" + secrets.token_hex(3).upper(),
                session_id=session_id,
                customer_id=customer_id,
                channel=channel,
                reason=reason,
                summary=summary[:2000],
                created_at=utc_now(),
            )
            session.add(ticket)
            await session.commit()
            return _row(ticket)

    async def list_handoffs(self, status: str | None = None) -> list[dict]:
        maker = await self._maker()
        async with maker() as session:
            query = select(Handoff).order_by(Handoff.created_at.desc()).limit(200)
            if status:
                query = query.where(Handoff.status == status)
            return [_row(r) for r in (await session.execute(query)).scalars()]

    async def get_handoff(self, handoff_id: str) -> dict | None:
        maker = await self._maker()
        async with maker() as session:
            row = await session.get(Handoff, handoff_id)
            return _row(row) if row else None

    async def active_handoff(self, session_id: str) -> dict | None:
        maker = await self._maker()
        async with maker() as session:
            row = (
                await session.execute(
                    select(Handoff).where(Handoff.session_id == session_id, Handoff.status == "claimed")
                )
            ).scalars().first()
            return _row(row) if row else None

    async def update_handoff(self, handoff_id: str, status: str, operator: str | None = None) -> dict | None:
        maker = await self._maker()
        async with maker() as session:
            row = await session.get(Handoff, handoff_id)
            if row is None:
                return None
            row.status = status
            if operator:
                row.claimed_by = operator
            if status == "closed":
                row.closed_at = utc_now()
            await session.commit()
            return _row(row)


async def reset_agent_stores() -> None:
    await _db.dispose()


# ------------------------------------------------------------------------------------------- sessions
@dataclass
class SessionInfo:
    session_id: str
    channel: str
    address: str
    customer_id: str | None = None
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


def default_session_id(channel: str, address: str) -> str:
    """Stable per contact, so WhatsApp/email/voice conversations survive restarts even without Redis."""
    return f"{channel}-{hashlib.sha256(address.strip().lower().encode()).hexdigest()[:12]}"


class MemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._by_address: dict[str, str] = {}
        self._outbox: dict[str, list[dict[str, Any]]] = {}

    async def get(self, session_id: str) -> SessionInfo | None:
        return self._sessions.get(session_id)

    async def get_or_create(self, channel: str, address: str, session_id: str | None = None) -> SessionInfo:
        key = f"{channel}:{address.lower()}"
        sid = session_id or self._by_address.get(key) or default_session_id(channel, address)
        info = self._sessions.get(sid)
        if info is None:
            info = SessionInfo(sid, channel, address)
            self._sessions[sid] = info
            self._by_address[key] = sid
        info.last_seen = time.time()
        return info

    async def save(self, info: SessionInfo) -> None:
        self._sessions[info.session_id] = info

    async def push(self, session_id: str, message: dict[str, Any]) -> None:
        box = self._outbox.setdefault(session_id, [])
        box.append({**message, "seq": len(box) + 1})

    async def messages(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        return [m for m in self._outbox.get(session_id, []) if m["seq"] > after]

    async def all_sessions(self) -> list[SessionInfo]:
        return sorted(self._sessions.values(), key=lambda s: s.last_seen, reverse=True)


class RedisSessionStore:
    """Session metadata + outbox in Redis, with a sliding TTL."""

    def __init__(self, url: str, ttl_s: int) -> None:
        import redis.asyncio as redis

        self.r = redis.from_url(url, decode_responses=True)
        self.ttl = ttl_s

    async def get(self, session_id: str) -> SessionInfo | None:
        raw = await self.r.get(f"concierge:session:{session_id}")
        return SessionInfo(**json.loads(raw)) if raw else None

    async def get_or_create(self, channel: str, address: str, session_id: str | None = None) -> SessionInfo:
        key = f"concierge:addr:{channel}:{address.lower()}"
        sid = session_id or await self.r.get(key) or default_session_id(channel, address)
        info = await self.get(sid) or SessionInfo(sid, channel, address)
        info.last_seen = time.time()
        await self.save(info)
        await self.r.set(key, sid, ex=self.ttl)
        return info

    async def save(self, info: SessionInfo) -> None:
        await self.r.set(f"concierge:session:{info.session_id}", json.dumps(asdict(info)), ex=self.ttl)
        await self.r.zadd("concierge:sessions", {info.session_id: info.last_seen})

    async def push(self, session_id: str, message: dict[str, Any]) -> None:
        key = f"concierge:outbox:{session_id}"
        seq = await self.r.incr(f"{key}:seq")
        await self.r.rpush(key, json.dumps({**message, "seq": seq}, default=str))
        await self.r.expire(key, self.ttl)
        await self.r.expire(f"{key}:seq", self.ttl)

    async def messages(self, session_id: str, after: int = 0) -> list[dict[str, Any]]:
        raw = await self.r.lrange(f"concierge:outbox:{session_id}", 0, -1)
        return [m for m in (json.loads(x) for x in raw) if m["seq"] > after]

    async def all_sessions(self) -> list[SessionInfo]:
        ids = await self.r.zrevrange("concierge:sessions", 0, 99)
        out = []
        for sid in ids:
            info = await self.get(sid)
            if info:
                out.append(info)
        return out


def make_session_store() -> MemorySessionStore | RedisSessionStore:
    settings = get_settings()
    if settings.redis_url:
        return RedisSessionStore(settings.redis_url, settings.session_ttl_s)
    return MemorySessionStore()
