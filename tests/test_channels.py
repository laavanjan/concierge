"""Gateway: every channel lands on the same agent core; webhooks are authenticated."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from channels.email_channel import parse_email, strip_reply
from channels.whatsapp import twilio_signature


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-secret")
    monkeypatch.setenv("META_APP_SECRET", "meta-secret")
    monkeypatch.setenv("META_VERIFY_TOKEN", "verify-me")
    from common.config import reset_settings

    reset_settings()
    from channels.gateway import create_app

    app = create_app(start_background=False)
    # Like uvicorn, run the whole lifespan inside one task (the MCP session manager owns an anyio task group).
    started, stop = asyncio.Event(), asyncio.Event()
    failure: list[BaseException] = []

    async def host() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)
            started.set()

    task = asyncio.create_task(host())
    await started.wait()
    if failure:
        raise failure[0]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30) as c:
            c.app = app  # type: ignore[attr-defined]
            yield c
    finally:
        stop.set()
        await task


OP = {"Authorization": "Bearer operator-dev-key", "X-Operator": "maya@aurora-bay.example"}


async def test_web_chat_and_sse_stream(client):
    r = await client.post("/v1/chat", json={"text": "can I bring my dog?", "session_id": "web-a"})
    body = r.json()
    assert r.status_code == 200 and body["agents"] == ["policy"] and body["citations"]
    r = await client.post("/v1/chat/stream", json={"text": "book 12-15 Dec for 2", "session_id": "web-a"})
    events = [line.split(": ", 1)[1] for line in r.text.splitlines() if line.startswith("event: ")]
    assert "progress" in events and events[-1] == "message"
    final = json.loads([line for line in r.text.splitlines() if line.startswith("data: ")][-1][6:])
    assert "Which room" in final["text"]
    transcript = (await client.get("/v1/sessions/web-a/transcript")).json()["transcript"]
    assert len(transcript) == 4


async def test_twilio_whatsapp_requires_valid_signature(client):
    params = {"From": "whatsapp:+15550123", "Body": "Is breakfast included?", "MessageSid": "SM1"}
    bad = await client.post("/channels/whatsapp/twilio", data=params, headers={"X-Twilio-Signature": "nope"})
    assert bad.status_code == 403
    sig = twilio_signature("http://localhost:8000/channels/whatsapp/twilio", params, "twilio-secret")
    ok = await client.post("/channels/whatsapp/twilio", data=params, headers={"X-Twilio-Signature": sig})
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("application/xml")
    assert "<Message>" in ok.text and "18 USD" in ok.text and "Sources:" in ok.text


async def test_meta_whatsapp_verify_and_signed_webhook(client):
    v = await client.get("/channels/whatsapp/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert v.text == "42"
    payload = {"entry": [{"changes": [{"value": {"messages": [{"from": "15550999", "id": "wamid.1", "type": "text", "text": {"body": "what time is check-in?"}}]}}]}]}
    raw = json.dumps(payload).encode()
    unsigned = await client.post("/channels/whatsapp/meta", content=raw, headers={"Content-Type": "application/json"})
    assert unsigned.status_code == 403
    sig = "sha256=" + hmac.new(b"meta-secret", raw, hashlib.sha256).hexdigest()
    signed = await client.post("/channels/whatsapp/meta", content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert signed.json() == {"status": "accepted"}


async def test_email_inbound_threads_and_formats_reply(client):
    r = await client.post(
        "/channels/email/inbound",
        json={"from_address": "Jo@Example.com", "name": "Jo Park", "subject": "Parking",
              "text": "Do you have parking?\n\nOn Mon, Front Desk wrote:\n> old message"},
    )
    reply = r.json()["reply"]
    assert reply.startswith("Hi Jo,") and "15 USD" in reply and "Sources:" in reply


def test_email_parsing_strips_quotes_and_signatures():
    raw = (b"From: Jo Park <jo@example.com>\r\nSubject: Re: booking\r\nMessage-ID: <m1@x>\r\n\r\n"
           b"Can I change my dates?\r\n--\r\nJo\r\n\r\nOn Tue, Concierge wrote:\r\n> earlier")
    parsed = parse_email(raw)
    assert parsed["from"] == "jo@example.com" and parsed["text"] == "Can I change my dates?"
    assert strip_reply("Thanks!\n> quoted") == "Thanks!"


async def test_voice_text_endpoint_returns_speakable_reply(client):
    r = await client.post("/v1/voice/text", json={"transcript": "what time is check out", "caller": "+15550111"})
    body = r.json()
    assert "11:00" in body["speech"] and "[" not in body["speech"]
    twiml = await client.post("/channels/voice/twilio/gather", data={"SpeechResult": "is there wifi?", "From": "+15550111"})
    assert "<Gather" in twiml.text and "wifi" in twiml.text.lower()


async def test_operator_api_needs_key_and_approves(client):
    assert (await client.get("/operator/api/approvals")).status_code == 401
    say = lambda text: client.post("/v1/chat", json={"text": text, "session_id": "web-op"})  # noqa: E731
    await say("book 12-15 Dec for 2")
    await say("the standard queen")
    booked = (await say("yes, Alex Morgan, alex@example.com")).json()["text"]
    rid = booked.split("reference is ")[1].split(":")[0]
    pending = (await say(f"I'd like a refund of $25 on {rid}, the shower was cold")).json()
    assert pending["status"] == "pending_approval"
    queue = (await client.get("/operator/api/approvals", headers=OP)).json()
    assert queue[0]["id"] == pending["approval_id"]
    done = (await client.post(f"/operator/api/approvals/{pending['approval_id']}/approve", headers=OP, json={})).json()
    assert done["status"] == "approved" and "approved" in done["message_to_customer"]
    again = await client.post(f"/operator/api/approvals/{pending['approval_id']}/approve", headers=OP, json={})
    assert again.status_code == 409
    outbox = (await client.get("/v1/sessions/web-op/messages")).json()["messages"]
    assert outbox[-1]["text"] == done["message_to_customer"]


async def test_ops_endpoints(client):
    await client.post("/v1/chat", json={"text": "hello", "session_id": "web-ops"})
    assert (await client.get("/readyz")).json()["status"] == "ready"
    stats = (await client.get("/ops/api/stats")).json()
    assert stats["turns"] >= 1 and "web" in stats["channels"]
    metrics = (await client.get("/metrics")).text
    assert "concierge_turn_latency_seconds" in metrics
    traces = (await client.get("/ops/api/traces")).json()
    assert traces and traces[0]["root"]
    index = await client.get("/")
    assert index.status_code == 200 and "Concierge" in index.text


async def test_mcp_server_is_reachable_over_streamable_http(client):
    r = await client.post(
        "/mcp/",
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                 "MCP-Protocol-Version": "2025-06-18"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
    )
    assert r.status_code == 200, r.text
    assert "concierge-backend" in r.text
