"""Build model clients from 'provider:model' specs, e.g. anthropic:claude-opus-5-5 or ollama:qwen2.5:3b."""

from __future__ import annotations

from agents.llm.base import ChatModel
from common.config import get_settings

_cache: dict[str, ChatModel] = {}


def build_model(spec: str | None) -> ChatModel | None:
    """Return a client, or None for 'rules'/'offline' (deterministic code paths)."""
    if not spec or spec in ("rules", "offline", "none"):
        return None
    if spec in _cache:
        return _cache[spec]
    provider, _, model = spec.partition(":")
    settings = get_settings()
    if provider == "anthropic":
        if not settings.use_llm:
            return None
        from agents.llm.anthropic_model import AnthropicChatModel

        client: ChatModel = AnthropicChatModel(model, timeout=settings.llm_timeout_s)
    elif provider == "ollama":
        from agents.llm.ollama_model import OllamaChatModel

        client = OllamaChatModel(model, settings.ollama_base_url)
    else:
        raise ValueError(f"unknown model provider in {spec!r}")
    _cache[spec] = client
    return client


def reset_models() -> None:
    _cache.clear()
