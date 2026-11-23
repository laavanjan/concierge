"""Claude via the official Anthropic Python SDK.

* prompt caching: a breakpoint on the frozen system prompt (shared by every
  session) plus top-level automatic caching for the growing conversation
* effort control (`output_config.effort`) instead of thinking budgets
* server-side refusal fallbacks (`fallbacks="default"`) on models that support them
* typed error handling: transient errors -> LLMUnavailable (caller falls back
  to the next model), request errors -> LLMBadRequest
* preserved-thinking recovery: if a replayed thinking block is rejected, strip
  thinking blocks once and retry (the documented one-time recovery)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import anthropic
from pydantic import BaseModel

from agents.llm.base import LLMBadRequest, LLMResponse, LLMUnavailable, ToolCall
from common.costs import Usage, cost_usd
from common.metrics import LLM_COST, LLM_LATENCY, LLM_TOKENS
from common.telemetry import span

log = logging.getLogger(__name__)

SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MID_CONVERSATION_SYSTEM_MODELS = {
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5-5", "claude-haiku-5-5",
    "claude-fable-5", "claude-fable-5-1", "claude-fable-5-2",
}
_DROP_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


def echo_safe(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepare response content for replay: before the last `fallback` boundary, drop model-internal blocks."""
    last = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    if last < 0:
        return content
    return [b for i, b in enumerate(content) if i >= last or b.get("type") not in _DROP_BEFORE_FALLBACK]


def strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = [b for b in content if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
        out.append({**message, "content": content})
    return out


class AnthropicChatModel:
    provider = "anthropic"

    def __init__(self, model: str, client: anthropic.AsyncAnthropic | None = None, timeout: float = 30.0) -> None:
        self.model = model
        self.client = client or anthropic.AsyncAnthropic(timeout=timeout, max_retries=2)

    @property
    def supports_system_messages(self) -> bool:
        return self.model in MID_CONVERSATION_SYSTEM_MODELS

    def _system(self, system: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    async def _create(self, kwargs: dict[str, Any]) -> Any:
        if self.model in SERVER_FALLBACK_MODELS:
            return await self.client.beta.messages.create(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
        return await self.client.messages.create(**kwargs)

    async def _call(self, kwargs: dict[str, Any], purpose: str, parse_schema: type[BaseModel] | None = None) -> Any:
        try:
            if parse_schema is not None:
                return await self.client.messages.parse(**kwargs, output_format=parse_schema)
            return await self._create(kwargs)
        except anthropic.BadRequestError as exc:
            message = str(exc)
            if "signature" in message and "thinking" in message and parse_schema is None:
                log.warning("thinking block rejected (%s); stripping thinking and retrying once", purpose)
                kwargs = {**kwargs, "messages": strip_thinking(kwargs["messages"])}
                return await self._create(kwargs)
            raise LLMBadRequest(message) from exc
        except (anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            raise LLMUnavailable(f"{self.model}: {exc.status_code}") from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise LLMUnavailable(f"{self.model}: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code in (408, 409, 429, 529):
                raise LLMUnavailable(f"{self.model}: {exc.status_code}") from exc
            raise LLMBadRequest(str(exc)) from exc

    def _to_response(self, resp: Any, started: float, purpose: str) -> LLMResponse:
        content = [block.to_dict(mode="json") for block in resp.content]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        tool_calls = [ToolCall(b["id"], b["name"], dict(b.get("input") or {})) for b in content if b.get("type") == "tool_use"]
        citations = []
        for b in content:
            if b.get("type") == "text" and b.get("citations"):
                citations.append({"text": b.get("text", ""), "citations": b["citations"]})
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        served_model = getattr(resp, "model", self.model) or self.model
        iterations = getattr(u, "iterations", None) or []
        fallback = any(getattr(it, "type", None) == "fallback_message" for it in iterations)
        latency = (time.perf_counter() - started) * 1000
        cost = cost_usd(served_model, usage)
        LLM_LATENCY.labels(served_model, purpose).observe(latency / 1000)
        LLM_TOKENS.labels(served_model, "input").inc(usage.input_tokens)
        LLM_TOKENS.labels(served_model, "output").inc(usage.output_tokens)
        LLM_TOKENS.labels(served_model, "cache_read").inc(usage.cache_read_tokens)
        LLM_COST.labels(served_model).inc(cost)
        return LLMResponse(
            content=echo_safe(content),
            text=text,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason or "end_turn",
            model=served_model,
            usage=usage,
            latency_ms=latency,
            cost_usd=cost,
            citations=citations,
            served_by_fallback=fallback,
        )

    async def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        effort: str | None = None,
        purpose: str = "agent",
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": self._system(system),
            "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            kwargs["tools"] = tools
        if effort:
            kwargs["output_config"] = {"effort": effort}
        with span("llm.generate", **{"gen_ai.system": "anthropic", "gen_ai.request.model": self.model, "llm.purpose": purpose}) as s:
            started = time.perf_counter()
            resp = await self._call(kwargs, purpose)
            result = self._to_response(resp, started, purpose)
            s.set_attribute("gen_ai.response.model", result.model)
            s.set_attribute("gen_ai.usage.input_tokens", result.usage.input_tokens)
            s.set_attribute("gen_ai.usage.output_tokens", result.usage.output_tokens)
            s.set_attribute("llm.cost_usd", result.cost_usd)
            s.set_attribute("llm.stop_reason", result.stop_reason)
            return result

    async def parse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        max_tokens: int = 1024,
        purpose: str = "router",
    ) -> tuple[BaseModel, LLMResponse]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": self._system(system),
            "messages": messages,
            "output_config": {"effort": "low"},
        }
        with span("llm.parse", **{"gen_ai.system": "anthropic", "gen_ai.request.model": self.model, "llm.purpose": purpose}):
            started = time.perf_counter()
            resp = await self._call(kwargs, purpose, parse_schema=schema)
            result = self._to_response(resp, started, purpose)
            parsed = getattr(resp, "parsed_output", None)
            if parsed is None:
                if result.refused:
                    raise LLMBadRequest("structured output refused")
                parsed = schema.model_validate_json(result.text)
            return parsed, result
