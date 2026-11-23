"""Output guardrail: check a draft reply against what the tools actually returned.

* prompt leak: canary token or long verbatim runs of a system prompt
* PII leak: email addresses that are not the customer's or the hotel's
* hallucinated money: amounts that appear in no tool result or cited document
* hallucinated booking references: reservation ids that no tool returned
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from common.metrics import GUARDRAIL_EVENTS
from guardrails.pii import EMAIL_RE

MONEY_RE = re.compile(
    r"(?:\$|USD\s?|US\$)\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"|(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?(?:USD|dollars)\b",
    re.I,
)
RESERVATION_RE = re.compile(r"\b[A-Z]{3}-[2-9A-HJ-NP-Z]{6}\b")  # generic shape; see reservation_ids()
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
LEAK_FALLBACK = "Sorry, I can't share that. How can I help with your stay?"
REFERENCE_FALLBACK = (
    "I wasn't able to confirm that booking reference in our system just now, so I've asked a colleague to check. "
    "You'll hear back shortly."
)


MONEY_KEYS = {
    "total", "subtotal", "tax", "amount", "refund_amount", "paid", "refunded", "price_difference", "indicative_total",
    "rate", "base_rate", "charge", "new_total", "previous_total", "extra_guest_fee",
}


@dataclass
class Evidence:
    amounts: set[float] = field(default_factory=set)
    money: set[float] = field(default_factory=set)
    reservation_ids: set[str] = field(default_factory=set)
    allowed_emails: set[str] = field(default_factory=set)

    def add_payload(self, payload: Any) -> None:
        """Collect every number and reservation id from a tool result or document."""
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key == "reservation_id" and isinstance(value, str):
                    self.reservation_ids.add(value.upper())
                if key in MONEY_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.money.add(round(float(value), 2))
                self.add_payload(value)
        elif isinstance(payload, list):
            for item in payload:
                self.add_payload(item)
        elif isinstance(payload, bool):
            return
        elif isinstance(payload, (int, float)):
            self.amounts.add(round(float(payload), 2))
        elif isinstance(payload, str):
            for match in NUMBER_RE.findall(payload.replace(",", "")):
                self.amounts.add(round(float(match), 2))
            for rid in reservation_ids(payload):
                self.reservation_ids.add(rid)

    def add_texts(self, texts: Iterable[str]) -> None:
        for text in texts:
            self.add_payload(text)


@dataclass
class OutputVerdict:
    text: str
    violations: list[str] = field(default_factory=list)
    blocked: bool = False


@lru_cache(maxsize=4)
def _reference_re(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(prefix)}-[2-9A-HJ-NP-Z]{{6}}\b")


def reservation_ids(text: str) -> list[str]:
    """Booking references in text. Only the property's own prefix counts (approval ids like APR-3B5C9D do not)."""
    from common.domain import domain

    return _reference_re(domain().property.reservation_prefix).findall(text.upper())


def _shingles(text: str, n: int) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def check_output(
    text: str,
    evidence: Evidence,
    canary: str | None = None,
    system_prompts: Iterable[str] = (),
    strict_amounts: bool = False,
) -> OutputVerdict:
    violations: list[str] = []
    if canary and canary in text:
        violations.append("prompt_leak:canary")
    else:
        reply_shingles = _shingles(text, 12)
        for prompt in system_prompts:
            if reply_shingles & _shingles(prompt, 12):
                violations.append("prompt_leak:verbatim")
                break
    if violations:
        for v in violations:
            GUARDRAIL_EVENTS.labels("output", v).inc()
        return OutputVerdict(LEAK_FALLBACK, violations, blocked=True)

    def _mask_foreign_email(match: re.Match[str]) -> str:
        email = match.group(0).lower()
        if email in evidence.allowed_emails:
            return match.group(0)
        violations.append("pii_leak:email")
        return "[email hidden]"

    text = EMAIL_RE.sub(_mask_foreign_email, text)

    unknown_ids = [rid for rid in reservation_ids(text) if rid not in evidence.reservation_ids]
    if unknown_ids:
        violations.append("unverified_reservation_id")
        for v in violations:
            GUARDRAIL_EVENTS.labels("output", v).inc()
        return OutputVerdict(REFERENCE_FALLBACK, violations, blocked=True)

    money = sorted(evidence.money)[:80]
    # differences between two backend money figures ("that's $34.65 less") are derived, not invented
    allowed = evidence.amounts | {round(abs(a - b), 2) for a in money for b in money if a != b}
    for match in MONEY_RE.finditer(text):
        raw = (match.group(1) or match.group(2) or "").replace(",", "")
        try:
            amount = round(float(raw), 2)
        except ValueError:
            continue
        if not any(abs(amount - known) <= 0.011 for known in allowed):
            violations.append("unverified_amount")
            break
    for v in violations:
        GUARDRAIL_EVENTS.labels("output", v).inc()
    blocked = strict_amounts and "unverified_amount" in violations
    if blocked:
        text = "Let me double-check that price for you before I confirm it."
    return OutputVerdict(text, violations, blocked)
