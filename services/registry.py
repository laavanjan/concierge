"""Service discovery: real URLs in compose/k8s, in-process ASGI apps in dev and tests.

`*_URL=inprocess` makes service-to-service calls go through httpx's ASGI
transport, so the whole backend runs inside one Python process with the exact
same HTTP contracts it uses when deployed as separate pods.
"""

from __future__ import annotations

from typing import Any

import httpx

from common.config import get_settings

SERVICES = ("inventory", "pricing", "reservations")
_clients: dict[str, httpx.AsyncClient] = {}


def service_app(name: str) -> Any:
    if name == "inventory":
        from services.inventory.app import app
    elif name == "pricing":
        from services.pricing.app import app
    elif name == "reservations":
        from services.reservations.app import app
    else:  # pragma: no cover - programming error
        raise KeyError(name)
    return app


def service_url(name: str) -> str:
    return getattr(get_settings(), f"{name}_url")


def service_client(name: str, timeout: float = 8.0) -> httpx.AsyncClient:
    client = _clients.get(name)
    if client is None or client.is_closed:
        url = service_url(name)
        if url == "inprocess":
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=service_app(name)),
                base_url=f"http://{name}.inprocess",
                timeout=timeout,
            )
        else:
            client = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=timeout)
        _clients[name] = client
    return client


async def close_clients() -> None:
    for client in list(_clients.values()):
        await client.aclose()
    _clients.clear()


async def reset_services() -> None:
    """Drop clients and per-service databases (used by tests and the eval harness)."""
    from services.inventory.app import db as inventory_db
    from services.pricing.app import db as pricing_db
    from services.reservations.app import db as reservations_db

    await close_clients()
    for db in (inventory_db, pricing_db, reservations_db):
        await db.dispose()
