"""Provider-neutral model interface used by the router, agents and the eval judge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from common.costs import Usage


class LLMError(Exception):
    """Base class for model failures."""


class LLMUnavailable(LLMError):
    """Transient failure (network, 429, 5xx, timeout): try the fallback model."""


class LLMBadRequest(LLMError):
    """The request itself was rejected (4xx): retrying the same body will not help."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    citations: list[dict[str, Any]] = field(default_factory=list)
    served_by_fallback: bool = False

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"

    def usage_record(self, purpose: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "purpose": purpose,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": round(self.latency_ms, 1),
            "stop_reason": self.stop_reason,
        }


class ChatModel(Protocol):
    provider: str
    model: str

    @property
    def supports_system_messages(self) -> bool: ...

    async def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        effort: str | None = None,
        purpose: str = "agent",
    ) -> LLMResponse: ...

    async def parse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        max_tokens: int = 1024,
        purpose: str = "router",
    ) -> tuple[BaseModel, LLMResponse]: ...
