"""Heuristic prompt-injection and jailbreak detector.

Scores text with weighted signals combined by noisy-OR, and re-scans
normalised variants (leetspeak, base64 payloads, zero-width-stripped) so cheap
obfuscation does not slip through. It is a first line of defence; the real
protection is architectural (tool allow-lists, approval tokens checked by the
backend, documents passed as data, output validation).
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field

ZERO_WIDTH = re.compile(r"[​-‏⁠-⁤﻿­]")
B64_RE = re.compile(r"\b[A-Za-z0-9+/]{20,}={0,2}")
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})

_NOUNS = r"(instructions?|rules?|prompts?|directions?|guidelines?|policies|constraints?|programming|guardrails?)"

SIGNALS: list[tuple[str, float, re.Pattern[str]]] = [
    (
        "override_instructions",
        0.85,
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass|skip|drop)\b[^.!?\n]{0,40}?"
            r"\b(previous|prior|above|earlier|all|any|your|the|these|those|system|developer|original)\b[^.!?\n]{0,30}?\b"
            + _NOUNS
        ),
    ),
    (
        "override_instructions_ml",
        0.85,
        re.compile(
            r"ignora (todas )?(las )?instrucciones|ignore[zr]? (toutes )?les instructions|"
            r"ignoriere (alle )?(vorherigen |bisherigen )?anweisungen|ignore as instru[cç][oõ]es|"
            r"ignora (tutte )?le istruzioni|negeer (alle )?(vorige )?instructies"
        ),
    ),
    (
        "new_instructions",
        0.5,
        re.compile(r"\b(new|updated|revised|real)\s+(system\s+)?(instructions?|rules|system prompt)\b|from now on,? you\b"),
    ),
    (
        "role_hijack",
        0.55,
        re.compile(
            r"\byou are (now|no longer)\b|\bact as (an? )?(unrestricted|unfiltered|evil|different|new)\b|"
            r"\bpretend (to be|you are|you're)\b|\brole-?play as\b|\bimpersonate\b|\bswitch to\b[^.]{0,20}\bmode\b"
        ),
    ),
    (
        "prompt_exfiltration",
        0.8,
        re.compile(
            r"\b(reveal|show|print|repeat|output|display|tell me|what (is|are|was)|leak|dump|share|give me|copy|recite)\b"
            r"[^.!?\n]{0,40}?\b(system prompt|system message|initial prompt|hidden prompt|developer (message|prompt)|"
            r"your (system |initial |original |hidden )?(prompt|instructions|rules|guidelines|configuration)"
            r"(?!\s+(for|on|about|regarding|to)\b))"
        ),
    ),
    (
        "jailbreak_marker",
        0.8,
        re.compile(
            r"\b(dan|do anything now|developer mode|jailbreak|jailbroken|god mode|unfiltered mode|"
            r"no restrictions|without (any )?restrictions|unrestricted ai|evil mode|sudo mode)\b"
        ),
    ),
    (
        "delimiter_injection",
        0.6,
        re.compile(
            r"</?(system|assistant|instructions?|tool_use|tool_result|function_calls?|antml\S*)>|<\|im_(start|end)\|>|"
            r"\[/?inst\]|#{2,}\s*(system|instruction)|(^|\n)\s*(system|assistant|developer)\s*:"
        ),
    ),
    (
        "tool_forgery",
        0.6,
        re.compile(
            r"\b(issue_refund|request_refund|override_price|approval_token|cancel_booking|create_booking|"
            r"modify_booking|tool_use_id)\b|\b(call|invoke|execute|run|trigger)\b[^.]{0,25}\b(tool|function|api)\b"
        ),
    ),
    (
        "authority_claim",
        0.5,
        re.compile(
            r"\b(i am|i'm|this is)\s+(the |a |an |your )?(hotel |duty |general )?(manager|admin(istrator)?|developer|"
            r"owner|ceo|operator|anthropic|system administrator)\b|\b(authori[sz]ed|approved) by (the )?"
            r"(manager|management|admin|system)\b|\badmin(istrator)? (mode|override|access|command)\b"
        ),
    ),
    (
        "coercion",
        0.35,
        re.compile(
            r"\b(give|grant|issue|make)\b[^.]{0,20}\b(me|us)\b[^.]{0,30}\b(free|100% (off|discount)|full refund|"
            r"for nothing|at no cost|\$0)\b|\b(you must|you have to|you are required to)\b[^.]{0,40}\b(refund|discount|free)\b"
        ),
    ),
    (
        "assistant_directive",
        0.45,
        re.compile(
            r"\b(assistant|ai|chatbot|bot|model|llm|claude|gpt)s?\b[^.]{0,30}\b(must|should|shall|will|always|never)\b"
            r"[^.]{0,40}\b(tell|say|offer|give|reveal|respond|answer|ignore|refund)\b"
        ),
    ),
]


@dataclass
class InjectionVerdict:
    score: float
    signals: list[str] = field(default_factory=list)
    decoded_payloads: int = 0

    @property
    def blocked(self) -> bool:
        return self.score >= BLOCK_THRESHOLD

    @property
    def suspicious(self) -> bool:
        return self.score >= SUSPICIOUS_THRESHOLD


BLOCK_THRESHOLD = 0.75
SUSPICIOUS_THRESHOLD = 0.4


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = ZERO_WIDTH.sub("", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _decoded_variants(text: str) -> list[str]:
    out = []
    for blob in B64_RE.findall(text):
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False)
        except (binascii.Error, ValueError):
            continue
        decoded = raw.decode("utf-8", errors="ignore")
        printable = sum(ch.isprintable() for ch in decoded)
        if decoded and printable / max(len(decoded), 1) > 0.9 and re.search(r"[a-z]{3,}\s", decoded.lower()):
            out.append(decoded)
    return out


ADDRESSED_TO_AI = re.compile(
    r"\b(note|message|instruction|reminder)s? (to|for) (the )?(ai|assistant|chatbot|bot|llm|language model|model)s?\b|"
    r"\b(dear|attention|hey|hi) (the )?(ai|assistant|chatbot|bot|llm)s?\b|"
    r"\b(ai|assistant|chatbot|virtual agent|bot|llm|language model)s? (should|must|shall|will|always|never|are to|is to)\b|"
    r"\bif you are an? (ai|assistant|language model|llm|chatbot)\b"
)


def detect_document_injection(text: str) -> InjectionVerdict:
    """Stricter scoring for retrieved documents: policy text never legitimately addresses the AI reading it."""
    verdict = detect_injection(text)
    if ADDRESSED_TO_AI.search(normalize(text)):
        remaining = (1 - verdict.score) * (1 - 0.7)
        return InjectionVerdict(round(1 - remaining, 3), sorted({*verdict.signals, "addressed_to_ai"}), verdict.decoded_payloads)
    return verdict


def detect_injection(text: str) -> InjectionVerdict:
    if not text:
        return InjectionVerdict(0.0)
    base = normalize(text)
    raw_lines = ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text)).lower()  # keeps newlines for "system:" lines
    # spaced-out letters ("i g n o r e") are collapsed for one more pass
    collapsed = re.sub(r"\b(\w) (?=\w\b)", r"\1", base)
    variants = [base, raw_lines, base.translate(LEET), collapsed]
    decoded = _decoded_variants(text)
    variants += [normalize(d) for d in decoded]
    hits: dict[str, float] = {}
    for variant in variants:
        for name, weight, pattern in SIGNALS:
            if name not in hits and pattern.search(variant):
                hits[name] = weight
    if decoded and hits:
        hits["encoded_payload"] = 0.3
    remaining = 1.0
    for weight in hits.values():
        remaining *= 1 - weight
    return InjectionVerdict(round(1 - remaining, 3), sorted(hits), len(decoded))
