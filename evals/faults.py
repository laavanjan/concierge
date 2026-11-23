"""Stand-in models for fault injection (model outages are simulated, not waited for)."""

from __future__ import annotations

from typing import Any

from agents.llm.base import LLMResponse, LLMUnavailable
from common.costs import Usage


class DownModel:
    """A model endpoint that is unreachable."""

    provider = "anthropic"
    supports_system_messages = True

    def __init__(self, model: str = "claude-opus-5-5") -> None:
        self.model = model

    async def generate(self, **_: Any) -> LLMResponse:
        raise LLMUnavailable(f"{self.model}: 529 overloaded (injected)")

    async def parse(self, **_: Any):
        raise LLMUnavailable(f"{self.model}: 529 overloaded (injected)")


class CannedModel:
    """A healthy fallback model that answers with fixed text (keeps reliability evals free and deterministic)."""

    provider = "anthropic"
    supports_system_messages = True

    def __init__(self, text: str = "Happy to help. Which room would you like?", model: str = "claude-sonnet-5-5") -> None:
        self.model = model
        self.text = text

    async def generate(self, **_: Any) -> LLMResponse:
        return LLMResponse([{"type": "text", "text": self.text}], self.text, [], "end_turn", self.model, Usage(900, 40), 40, 0.0022)

    async def parse(self, **_: Any):
        raise LLMUnavailable("canned model does not parse")


class BrokenRetriever:
    async def search(self, *_: Any, **__: Any):
        raise ConnectionError("vector store unreachable (injected)")
