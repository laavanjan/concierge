"""Shared plumbing for the microservices: per-service database, error helpers, dialect-aware upserts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from common.db import make_engine, make_sessionmaker


class ServiceDB:
    """Lazily-initialised database owned by exactly one service (database-per-service)."""

    def __init__(
        self,
        url_getter: Callable[[], str],
        metadata: MetaData,
        on_init: Callable[[async_sessionmaker[AsyncSession]], Awaitable[None]] | None = None,
    ) -> None:
        self._url_getter = url_getter
        self._metadata = metadata
        self._on_init = on_init
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> async_sessionmaker[AsyncSession]:
        if self._sessionmaker is not None:
            return self._sessionmaker
        async with self._lock:
            if self._sessionmaker is None:
                engine = make_engine(self._url_getter())
                async with engine.begin() as conn:
                    await conn.run_sync(self._metadata.create_all)
                maker = make_sessionmaker(engine)
                if self._on_init:
                    await self._on_init(maker)
                self._engine, self._sessionmaker = engine, maker
        return self._sessionmaker

    async def session(self) -> AsyncIterator[AsyncSession]:
        maker = await self.ensure()
        async with maker() as session:
            yield session

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine, self._sessionmaker = None, None
        self._lock = asyncio.Lock()


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def http_error(status: int, error: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "message": message, **extra})


def insert_ignore(session: AsyncSession, table: Any, rows: list[dict[str, Any]]) -> Any:
    """INSERT ... ON CONFLICT DO NOTHING for SQLite and Postgres."""
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table).values(rows).on_conflict_do_nothing()
