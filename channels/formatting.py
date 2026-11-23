"""Render one OutboundMessage for each channel's constraints."""

from __future__ import annotations

import re

from common.domain import domain
from common.messages import OutboundMessage

WHATSAPP_LIMIT = 1500


def _sources(message: OutboundMessage) -> str:
    lines = []
    for i, c in enumerate(message.citations, start=1):
        page = f", p. {c['page']}" if c.get("page") else ""
        lines.append(f"[{i}] {c['title']}: {c['section']}{page}")
    return "\n".join(lines)


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    return re.sub(r"^#+\s*", "", text, flags=re.M)


def for_whatsapp(message: OutboundMessage) -> str:
    text = strip_markdown(message.text)
    if message.citations:
        text += "\n\nSources:\n" + _sources(message)
    if len(text) > WHATSAPP_LIMIT:
        text = text[: WHATSAPP_LIMIT - 40].rsplit(" ", 1)[0] + "... (reply MORE for the rest)"
    return text


def for_email(message: OutboundMessage, name: str | None = None) -> str:
    pack = domain()
    greeting = f"Hi {name.split()[0]}," if name else "Hello,"
    body = strip_markdown(message.text)
    sources = f"\n\nSources:\n{_sources(message)}" if message.citations else ""
    return (
        f"{greeting}\n\n{body}{sources}\n\nBest regards,\n{pack.property.name} concierge (automated assistant)\n"
        "Reply to this email to continue the conversation, or write 'human' to reach our team."
    )


def for_voice(message: OutboundMessage) -> str:
    text = re.sub(r"\s*\[\d+\]", "", strip_markdown(message.text))
    text = re.sub(r"^\s*-\s*", "", text, flags=re.M)
    text = text.replace("\n\n", " ").replace("\n", ". ")
    return re.sub(r"\.\s*\.", ".", text).strip()
