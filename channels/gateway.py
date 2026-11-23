"""Channel gateway: one FastAPI app that turns every channel into one message format.

    uvicorn channels.gateway:app --port 8000

In `inprocess` topology it also hosts the three backend services (/svc/...) and
the MCP server over Streamable HTTP (/mcp/), so the whole system runs in one
process for development and demos; in compose/k8s each runs separately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from agents.runtime import ConciergeRuntime
from agents.stores import reset_agent_stores
from channels import email_channel, operator, ops, voice, web, whatsapp
from channels.notifier import handle_event
from common.config import get_settings
from common.events import RedisStreamEventBus, get_event_bus
from common.logging import setup_logging
from common.telemetry import instrument_fastapi, setup_telemetry
from services.registry import reset_services

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "web" / "static"


def create_app(runtime: ConciergeRuntime | None = None, start_background: bool = True) -> FastAPI:
    settings = get_settings()
    rt = runtime or ConciergeRuntime(settings)
    mcp_http = mcp_manager = None
    if settings.mcp_url == "inprocess":
        from mcp.server.transport_security import TransportSecuritySettings

        from mcp_server.server import server as mcp_server

        mcp_http = mcp_server.streamable_http_app(
            streamable_http_path="/",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False, allowed_hosts=[], allowed_origins=[]
            ),
        )
        mcp_manager = mcp_server.session_manager  # one manager per app; run() is single-use

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        setup_telemetry("concierge-gateway")
        tasks: list[asyncio.Task] = []
        async with AsyncExitStack() as stack:
            if mcp_manager is not None:
                await stack.enter_async_context(mcp_manager.run())
            await rt.start()
            stack.push_async_callback(reset_agent_stores)
            stack.push_async_callback(reset_services)
            stack.push_async_callback(rt.stop)
            rt.register_sender("whatsapp", whatsapp.whatsapp_sender)
            rt.register_sender("email", email_channel.email_sender)
            bus = get_event_bus()
            if not isinstance(bus, RedisStreamEventBus):
                bus.subscribe(handle_event)  # with Redis, the worker consumes events instead
            if start_background and settings.imap_host and settings.imap_user:
                tasks.append(asyncio.create_task(email_channel.imap_poller(rt)))
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(
        title="Concierge Gateway",
        version="1.0.0",
        description="Multi-agent reservation assistant: web chat, WhatsApp, email and voice over one agent core.",
        lifespan=lifespan,
    )
    app.state.runtime = rt
    instrument_fastapi(app)
    for router in (web.router, whatsapp.router, email_channel.router, voice.router, operator.router, ops.router):
        app.include_router(router)

    if settings.inventory_url == "inprocess":
        from services.inventory.app import app as inventory_app
        from services.pricing.app import app as pricing_app
        from services.reservations.app import app as reservations_app

        app.mount("/svc/inventory", inventory_app)
        app.mount("/svc/pricing", pricing_app)
        app.mount("/svc/reservations", reservations_app)
    if mcp_http is not None:
        app.mount("/mcp", mcp_http)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/operator", include_in_schema=False)
    async def operator_console() -> FileResponse:
        return FileResponse(STATIC / "operator.html")

    @app.get("/ops", include_in_schema=False)
    async def ops_dashboard() -> FileResponse:
        return FileResponse(STATIC / "ops.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg")

    return app


app = create_app()
