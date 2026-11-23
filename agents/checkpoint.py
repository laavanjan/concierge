"""LangGraph checkpointer factory: conversation state + human-in-the-loop interrupts survive restarts.

memory   - InMemorySaver (tests)
sqlite   - AsyncSqliteSaver (local dev; default)
redis    - AsyncRedisSaver (production sessions; needs Redis 8 / Redis Stack, `pip install .[redis-checkpoint]`)
postgres - AsyncPostgresSaver (`pip install .[postgres]`)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from common.config import Settings


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[Any]:
    kind = settings.checkpointer
    if kind == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
    elif kind == "sqlite":
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_url or ":memory:") as saver:
            yield saver
    elif kind == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_url) as saver:
            await saver.setup()
            yield saver
    elif kind == "redis":
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver

        ttl = {"default_ttl": max(1, settings.session_ttl_s // 60), "refresh_on_read": True}  # minutes; sliding expiry
        async with AsyncRedisSaver.from_conn_string(settings.checkpoint_url or settings.redis_url, ttl=ttl) as saver:
            await saver.asetup()
            yield saver
    else:  # pragma: no cover - validated by Settings
        raise ValueError(f"unknown checkpointer {kind}")
