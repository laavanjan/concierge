"""PII detection and masking.

Two levels:
* `mask_secrets` removes data the assistant never needs (card numbers, CVV,
  IBAN, SSN, passport numbers) before text reaches a model, a log or a trace.
* `mask_for_logs` additionally masks emails and phone numbers, which the
  booking flow does need but logs and traces do not.
"""

from __future__ import annotations

import re

CARD_RE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
CVV_RE = re.compile(r"\b(cvv2?|cvc|security code|card code)\b\W{0,3}(?:is\s*)?\d{3,4}\b", re.I)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PASSPORT_RE = re.compile(r"\bpassport\s*(?:no\.?|number|#)?\s*(?:is\s*)?:?\s*([A-Z0-9]{6,9})\b", re.I)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<![\w-])\+?\d[\d\s().-]{7,}\d(?![\w-])")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_secrets(text: str) -> tuple[str, list[str]]:
    findings: list[str] = []

    def _card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            findings.append("pii:payment_card")
            return f"[CARD ending {digits[-4:]}]"
        return match.group(0)

    text = CARD_RE.sub(_card, text)
    if CVV_RE.search(text):
        findings.append("pii:cvv")
        text = CVV_RE.sub("[CVV removed]", text)
    if SSN_RE.search(text):
        findings.append("pii:ssn")
        text = SSN_RE.sub("[SSN removed]", text)
    if PASSPORT_RE.search(text):
        findings.append("pii:passport")
        text = PASSPORT_RE.sub("passport [removed]", text)

    def _iban(match: re.Match[str]) -> str:
        if sum(c.isdigit() for c in match.group(0)) >= 8:
            findings.append("pii:iban")
            return "[IBAN removed]"
        return match.group(0)

    text = IBAN_RE.sub(_iban, text)
    return text, findings


def find_emails(text: str) -> list[str]:
    return [m.lower() for m in EMAIL_RE.findall(text)]


def find_phones(text: str) -> list[str]:
    out = []
    for match in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", match)
        if 9 <= len(digits) <= 15 and not ISO_DATE_RE.match(match.strip()):
            out.append(match.strip())
    return out


def _mask_email(match: re.Match[str]) -> str:
    local, _, domain = match.group(0).partition("@")
    return f"{local[:1]}***@{domain}"


def _mask_phone(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    if not 9 <= len(digits) <= 15 or ISO_DATE_RE.match(raw.strip()):
        return raw
    return f"***{digits[-4:]}"


def mask_for_logs(text: str) -> str:
    text, _ = mask_secrets(text)
    text = EMAIL_RE.sub(_mask_email, text)
    return PHONE_RE.sub(_mask_phone, text)
