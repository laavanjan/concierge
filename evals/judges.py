"""Faithfulness judges: a deterministic lexical check (always) and an LLM judge (when a model is configured)."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from agents.llm.base import ChatModel, LLMError
from agents.prompts import render
from rag.text import tokenize


class FaithfulnessVerdict(BaseModel):
    supported: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    score: float


SKIP = re.compile(
    r"\b(i can connect you|front desk|don't want to guess|couldn't find|anything else|let me know|happy to help)\b", re.I
)


def lexical_support(answer: str, passages: list[str], threshold: float = 0.6) -> float:
    """Share of answer sentences whose content words are mostly present in the retrieved passages."""
    vocab = set(tokenize(" ".join(passages)))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\[\d+\]", "", answer)) if s.strip()]
    claims = [s for s in sentences if not SKIP.search(s) and len(tokenize(s)) >= 2]
    if not claims:
        return 1.0
    supported = 0
    for sentence in claims:
        words = set(tokenize(sentence))
        if words and len(words & vocab) / len(words) >= threshold:
            supported += 1
    return supported / len(claims)


async def llm_faithfulness(model: ChatModel, question: str, answer: str, passages: list[str]) -> FaithfulnessVerdict | None:
    system, _ref = render("judge")
    numbered = "\n\n".join(f"[passage {i + 1}]\n{p}" for i, p in enumerate(passages))
    try:
        verdict, _response = await model.parse(
            system=system,
            messages=[{"role": "user", "content": f"<passages>\n{numbered}\n</passages>\n<question>{question}</question>\n<answer>{answer}</answer>"}],
            schema=FaithfulnessVerdict,
            max_tokens=1500,
            purpose="judge",
        )
        return verdict  # type: ignore[return-value]
    except LLMError:
        return None
