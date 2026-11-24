"""Every test gets a fresh data dir, frozen 'today', offline models and in-memory checkpoints."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(autouse=True)
async def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONCIERGE_TODAY", "2026-11-24")
    monkeypatch.setenv("LLM_MODE", "offline")
    monkeypatch.setenv("CHECKPOINTER", "memory")
    monkeypatch.setenv("ROUTER_MODEL", "rules")
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret")
    for key in ("REDIS_URL", "QDRANT_URL", "OTEL_EXPORTER_OTLP_ENDPOINT", "SMTP_HOST", "IMAP_HOST", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    from agents.llm.factory import reset_models
    from agents.stores import reset_agent_stores
    from common.config import reset_settings
    from common.events import reset_event_bus
    from services.registry import reset_services

    reset_settings()
    reset_event_bus()
    reset_models()
    yield
    await reset_services()
    await reset_agent_stores()
    reset_settings()


@pytest.fixture
async def runtime():
    from agents.runtime import ConciergeRuntime

    rt = await ConciergeRuntime().start()
    yield rt
    await rt.stop()


@pytest.fixture
def say(runtime):
    from common.messages import InboundMessage

    async def _say(text: str, session: str = "web-test", channel: str = "web", address: str = "visitor-1"):
        return await runtime.handle(InboundMessage(channel=channel, address=address, text=text, session_id=session))

    return _say
