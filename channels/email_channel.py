"""Email channel: IMAP polling (or an inbound-parse webhook) in, SMTP out, threaded replies."""

from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agents.stores import SessionInfo
from channels.formatting import for_email
from common.config import get_settings
from common.messages import InboundMessage, OutboundMessage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/channels/email", tags=["email"])
QUOTE_MARKERS = [
    re.compile(r"^On .+wrote:\s*$", re.M),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.M | re.I),
    re.compile(r"^From: .+$", re.M),
    re.compile(r"^-- ?$", re.M),  # signature delimiter
    re.compile(r"^Sent from my ", re.M),
]


def strip_reply(text: str) -> str:
    """Keep only the new part of an email reply."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cut = len(text)
    for marker in QUOTE_MARKERS:
        m = marker.search(text)
        if m:
            cut = min(cut, m.start())
    lines = [ln for ln in text[:cut].splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def parse_email(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    if body_part is not None and body_part.get_content_type() == "text/html":
        body = re.sub(r"<[^>]+>", " ", body)
    return {
        "from": parseaddr(msg.get("From", ""))[1].lower(),
        "name": parseaddr(msg.get("From", ""))[0],
        "subject": msg.get("Subject", ""),
        "message_id": msg.get("Message-ID"),
        "text": strip_reply(body),
    }


class InboundEmail(BaseModel):
    from_address: str
    subject: str = ""
    text: str
    message_id: str | None = None
    name: str | None = None


def send_email(to: str, subject: str, body: str, in_reply_to: str | None = None) -> None:
    settings = get_settings()
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject or 'Your stay'}"
    msg["Message-ID"] = make_msgid(domain=settings.smtp_from.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    if not settings.smtp_host:
        log.info("[email dry-run] to=%s subject=%s\n%s", to, msg["Subject"], body[:300])
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(msg)


async def handle_email(runtime, parsed: dict) -> OutboundMessage | None:
    if not parsed["text"] or parsed["from"] == get_settings().smtp_from.lower():
        return None
    out = await runtime.handle(
        InboundMessage(channel="email", address=parsed["from"], text=parsed["text"][:4000], message_id=parsed.get("message_id"))
    )
    if out.text:
        body = for_email(out, parsed.get("name"))
        await asyncio.to_thread(send_email, parsed["from"], parsed.get("subject", ""), body, parsed.get("message_id"))
    return out


@router.post("/inbound")
async def inbound_webhook(request: Request, payload: InboundEmail) -> dict:
    """Inbound-parse webhook (SendGrid/Mailgun/Postmark style) and the easiest way to test the channel."""
    out = await handle_email(
        request.app.state.runtime,
        {"from": payload.from_address.lower(), "name": payload.name, "subject": payload.subject,
         "text": strip_reply(payload.text), "message_id": payload.message_id},
    )
    return {"reply": for_email(out, payload.name) if out and out.text else None, "status": out.status if out else "ignored"}


def _fetch_unseen() -> list[bytes]:
    settings = get_settings()
    messages = []
    with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as imap:
        imap.login(settings.imap_user, settings.imap_password or "")
        imap.select("INBOX")
        _typ, data = imap.search(None, "UNSEEN")
        for num in (data[0] or b"").split()[:20]:
            _typ, parts = imap.fetch(num, "(RFC822)")
            if parts and isinstance(parts[0], tuple):
                messages.append(parts[0][1])
            imap.store(num, "+FLAGS", "\\Seen")
    return messages


async def imap_poller(runtime) -> None:
    settings = get_settings()
    log.info("IMAP poller started for %s every %ss", settings.imap_user, settings.imap_poll_s)
    while True:
        try:
            for raw in await asyncio.to_thread(_fetch_unseen):
                await handle_email(runtime, parse_email(raw))
        except Exception:
            log.exception("IMAP poll failed")
        await asyncio.sleep(settings.imap_poll_s)


async def email_sender(session: SessionInfo, message: OutboundMessage) -> None:
    await asyncio.to_thread(send_email, session.address, "Update on your request", for_email(message))
