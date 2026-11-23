"""Input guardrail: normalise, cap length, mask secrets, score prompt injection."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from common.metrics import GUARDRAIL_EVENTS
from guardrails.injection import InjectionVerdict, detect_injection
from guardrails.pii import mask_secrets

CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ZERO_WIDTH = re.compile(r"[​-‏⁠-⁤﻿]")
MAX_CHARS = 4000

SAFE_REFUSAL = (
    "I can't help with that. I can book, change or cancel a stay, give you prices, "
    "or answer questions about the hotel's policies."
)
CARD_WARNING = (
    "For your security I've removed the card details from your message. Please never share card numbers here; "
    "we send a secure payment link after booking."
)


@dataclass
class InputVerdict:
    action: Literal["allow", "flag", "block"]
    text: str
    findings: list[str] = field(default_factory=list)
    injection: InjectionVerdict = field(default_factory=lambda: InjectionVerdict(0.0))

    @property
    def card_masked(self) -> bool:
        return any(f in ("pii:payment_card", "pii:cvv", "pii:iban") for f in self.findings)


def check_input(text: str, max_chars: int = MAX_CHARS) -> InputVerdict:
    findings: list[str] = []
    clean = unicodedata.normalize("NFKC", text or "")
    clean = ZERO_WIDTH.sub("", CONTROL.sub("", clean)).strip()
    if len(clean) > max_chars:
        clean = clean[:max_chars]
        findings.append("truncated")
    clean, pii = mask_secrets(clean)
    findings += pii
    verdict = detect_injection(text or "")
    if verdict.blocked:
        action: Literal["allow", "flag", "block"] = "block"
    elif verdict.suspicious:
        action = "flag"
    else:
        action = "allow"
    for finding in findings:
        GUARDRAIL_EVENTS.labels("input", finding).inc()
    if action != "allow":
        GUARDRAIL_EVENTS.labels("input", f"injection_{action}").inc()
    return InputVerdict(action, clean, findings, verdict)
