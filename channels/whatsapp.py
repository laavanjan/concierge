"""WhatsApp adapters: Twilio sandbox (TwiML replies) and Meta Cloud API (webhook + Graph API sends).

Both validate the provider's webhook signature before anything reaches the agents.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from agents.stores import SessionInfo
from channels.formatting import for_whatsapp
from common.config import get_settings
from common.messages import InboundMessage, OutboundMessage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/channels/whatsapp", tags=["whatsapp"])
GRAPH_URL = "https://graph.facebook.com/v21.0"


def twilio_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    data = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def meta_signature_ok(body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def twiml(text: str) -> Response:
    body = f'<?xml version="1.0" encoding="UTF-8"?><Response>{f"<Message>{escape(text)}</Message>" if text else ""}</Response>'
    return Response(content=body, media_type="application/xml")


@router.post("/twilio")
async def twilio_webhook(request: Request) -> Response:
    settings = get_settings()
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if settings.twilio_auth_token and settings.twilio_validate_signature:
        url = settings.public_base_url.rstrip("/") + request.url.path
        expected = twilio_signature(url, params, settings.twilio_auth_token)
        if not hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
    sender = params.get("From", "").removeprefix("whatsapp:")
    text = params.get("Body", "").strip()
    if not sender or not text:
        return twiml("")
    runtime = request.app.state.runtime
    out = await runtime.handle(
        InboundMessage(channel="whatsapp", address=sender, text=text, message_id=params.get("MessageSid"))
    )
    return twiml(for_whatsapp(out) if out.text else "")


@router.get("/meta")
async def meta_verify(request: Request) -> Response:
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == get_settings().meta_verify_token:
        return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/meta")
async def meta_webhook(request: Request, background: BackgroundTasks) -> dict:
    settings = get_settings()
    body = await request.body()
    if settings.meta_app_secret and not meta_signature_ok(body, request.headers.get("X-Hub-Signature-256"), settings.meta_app_secret):
        raise HTTPException(status_code=403, detail="invalid signature")
    payload = await request.json()
    runtime = request.app.state.runtime
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("type") != "text":
                    continue
                inbound = InboundMessage(
                    channel="whatsapp", address=msg["from"], text=msg["text"]["body"], message_id=msg.get("id")
                )

                async def process(inbound: InboundMessage = inbound) -> None:
                    out = await runtime.handle(inbound)
                    if out.text:
                        await send_meta(inbound.address, for_whatsapp(out))

                background.add_task(process)  # ack fast; Meta retries slow webhooks
    return {"status": "accepted"}


async def send_meta(to: str, text: str) -> None:
    settings = get_settings()
    if not (settings.meta_access_token and settings.meta_phone_number_id):
        log.info("[whatsapp/meta dry-run] to=%s: %s", to, text[:120])
        return
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{GRAPH_URL}/{settings.meta_phone_number_id}/messages",
            headers={"Authorization": f"Bearer {settings.meta_access_token}"},
            json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}},
        )
        resp.raise_for_status()


async def send_twilio(to: str, text: str) -> None:
    settings = get_settings()
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        log.info("[whatsapp/twilio dry-run] to=%s: %s", to, text[:120])
        return
    async with httpx.AsyncClient(timeout=10, auth=(settings.twilio_account_sid, settings.twilio_auth_token)) as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json",
            data={"From": settings.twilio_whatsapp_from, "To": f"whatsapp:{to}", "Body": text},
        )
        resp.raise_for_status()


async def whatsapp_sender(session: SessionInfo, message: OutboundMessage) -> None:
    """Push an asynchronous message (approval outcome, operator reply) back over WhatsApp."""
    settings = get_settings()
    text = for_whatsapp(message)
    if settings.meta_access_token:
        await send_meta(session.address, text)
    else:
        await send_twilio(session.address, text)
