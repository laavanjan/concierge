"""A scripted stand-in for Claude so the LLM code paths run in CI without API keys."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from agents.llm.base import LLMResponse, LLMUnavailable, ToolCall
from common.costs import Usage

_ids = itertools.count(1)
Step = LLMResponse | Callable[[list[dict[str, Any]]], LLMResponse] | Exception


def tool_use(name: str, args: dict[str, Any], thinking: bool = True) -> LLMResponse:
    call_id = f"toolu_{next(_ids):04d}"
    content = ([{"type": "thinking", "thinking": "", "signature": "sig"}] if thinking else []) + [
        {"type": "tool_use", "id": call_id, "name": name, "input": args}
    ]
    return LLMResponse(content, "", [ToolCall(call_id, name, args)], "tool_use", "claude-opus-5-5", Usage(1200, 60), 50, 0.006)


def reply(text: str, citations: list[dict[str, Any]] | None = None, stop: str = "end_turn") -> LLMResponse:
    block: dict[str, Any] = {"type": "text", "text": text}
    if citations:
        block["citations"] = citations
    return LLMResponse([block], text, [], stop, "claude-opus-5-5", Usage(1500, 80), 60, 0.0076)


class ScriptedModel:
    provider = "anthropic"
    supports_system_messages = True

    def __init__(self, steps: list[Step] | None = None, model: str = "claude-opus-5-5", parsed: BaseModel | None = None):
        self.model = model
        self.steps = list(steps or [])
        self.parsed = parsed
        self.calls: list[dict[str, Any]] = []

    async def generate(self, *, system: str, messages: list[dict[str, Any]], tools=None, max_tokens=4096, effort=None, purpose="agent"):
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools, "purpose": purpose, "effort": effort})
        if not self.steps:
            raise AssertionError(f"ScriptedModel ran out of steps ({purpose})")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step

    async def parse(self, *, system, messages, schema, max_tokens=1024, purpose="router"):
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "purpose": purpose})
        if self.parsed is None:
            raise LLMUnavailable("no parse result scripted")
        return self.parsed, reply("{}")


def last_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in reversed(messages):
        if message["role"] == "user" and isinstance(message["content"], list):
            return [b for b in message["content"] if b.get("type") == "tool_result"]
    return []
