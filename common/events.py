"""Event bus: Redis Streams in production, in-memory fan-out for dev and tests.

Services publish domain events (reservation.created, refund.issued, ...); the
gateway/worker consumes them, e.g. to deliver a message after an operator
approves a refund, or to send a confirmation email.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from common.config import get_settings

log = logging.getLogger(__name__)
Handler = Callable[[dict[str, Any]], Awaitable[None]]
STREAM = "concierge:events"


def make_event(event_type: str, payload: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "source": source,
        "ts": datetime.now(tz=UTC).isoformat(),
        "payload": payload,
    }


class InMemoryEventBus:
    def __init__(self, keep: int = 500) -> None:
        self.recent: deque[dict[str, Any]] = deque(maxlen=keep)
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def publish(self, event_type: str, payload: dict[str, Any], source: str = "concierge") -> dict[str, Any]:
        event = make_event(event_type, payload, source)
        self.recent.append(event)
        for handler in list(self._handlers):
            try:
                await handler(event)
            except Exception:  # a bad consumer must never break the producer
                log.exception("event handler failed for %s", event_type)
        return event

    async def close(self) -> None:
        return None


class RedisStreamEventBus(InMemoryEventBus):
    """XADD on publish; `run_consumer` reads with a consumer group (at-least-once)."""

    def __init__(self, url: str) -> None:
        super().__init__()
        import redis.asyncio as redis

        self._redis = redis.from_url(url, decode_responses=True)

    async def publish(self, event_type: str, payload: dict[str, Any], source: str = "concierge") -> dict[str, Any]:
        event = make_event(event_type, payload, source)
        self.recent.append(event)
        try:
            await self._redis.xadd(STREAM, {"event": json.dumps(event, default=str)}, maxlen=10_000)
        except Exception:
            log.exception("failed to publish %s to redis; delivering in-process only", event_type)
        for handler in list(self._handlers):
            with contextlib.suppress(Exception):
                await handler(event)
        return event

    async def run_consumer(self, group: str, consumer: str, handler: Handler) -> None:
        with contextlib.suppress(Exception):
            await self._redis.xgroup_create(STREAM, group, id="$", mkstream=True)
        while True:
            batches = await self._redis.xreadgroup(group, consumer, {STREAM: ">"}, count=20, block=5000)
            for _stream, entries in batches or []:
                for entry_id, fields in entries:
                    try:
                        await handler(json.loads(fields["event"]))
                        await self._redis.xack(STREAM, group, entry_id)
                    except Exception:
                        log.exception("consumer %s failed on %s", consumer, entry_id)
                        await asyncio.sleep(0.5)

    async def close(self) -> None:
        await self._redis.aclose()


_bus: InMemoryEventBus | None = None


def get_event_bus() -> InMemoryEventBus:
    global _bus
    if _bus is None:
        url = get_settings().redis_url
        _bus = RedisStreamEventBus(url) if url else InMemoryEventBus()
    return _bus


def reset_event_bus() -> None:
    global _bus
    _bus = None
