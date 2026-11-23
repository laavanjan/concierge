"""Small open model (Qwen / Llama) served by Ollama, used for routing and classification.

Uses Ollama's native /api/chat with a JSON schema in `format`, so the small
model returns the same Pydantic-validated RouterDecision as Claude would.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from pydantic import BaseModel

from agents.llm.base import LLMBadRequest, LLMResponse, LLMUnavailable
from common.costs import Usage
from common.metrics import LLM_LATENCY, LLM_TOKENS
from common.telemetry import span


def _flatten(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        out.append({"role": m["role"], "content": content or ""})
    return out


class OllamaChatModel:
    provider = "ollama"
    supports_system_messages = True

    def __init__(self, model: str, base_url: str, timeout: float = 20.0) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _chat(self, payload: dict[str, Any], purpose: str) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.base_url}/api/chat", json=payload)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise LLMUnavailable(f"ollama unreachable: {exc}") from exc
        if resp.status_code >= 500:
            raise LLMUnavailable(f"ollama {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMBadRequest(resp.text[:300])
        latency = (time.perf_counter() - started) * 1000
        LLM_LATENCY.labels(self.model, purpose).observe(latency / 1000)
        return resp.json(), latency

    def _response(self, data: dict[str, Any], latency: float) -> LLMResponse:
        text = data.get("message", {}).get("content", "")
        usage = Usage(input_tokens=data.get("prompt_eval_count", 0), output_tokens=data.get("eval_count", 0))
        LLM_TOKENS.labels(self.model, "input").inc(usage.input_tokens)
        LLM_TOKENS.labels(self.model, "output").inc(usage.output_tokens)
        return LLMResponse(
            content=[{"type": "text", "text": text}],
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            model=f"ollama:{self.model}",
            usage=usage,
            latency_ms=latency,
            cost_usd=0.0,
        )

    async def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
        effort: str | None = None,
        purpose: str = "agent",
    ) -> LLMResponse:
        if tools:
            raise LLMBadRequest("the small-model client is used for routing/classification only (no tools)")
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *_flatten(messages)],
            "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        with span("llm.generate", **{"gen_ai.system": "ollama", "gen_ai.request.model": self.model, "llm.purpose": purpose}):
            data, latency = await self._chat(payload, purpose)
            return self._response(data, latency)

    async def parse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        max_tokens: int = 1024,
        purpose: str = "router",
    ) -> tuple[BaseModel, LLMResponse]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *_flatten(messages)],
            "format": schema.model_json_schema(),
            "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        with span("llm.parse", **{"gen_ai.system": "ollama", "gen_ai.request.model": self.model, "llm.purpose": purpose}):
            data, latency = await self._chat(payload, purpose)
            result = self._response(data, latency)
            try:
                return schema.model_validate(json.loads(result.text)), result
            except (json.JSONDecodeError, ValueError) as exc:
                raise LLMBadRequest(f"small model returned invalid JSON: {result.text[:200]}") from exc
