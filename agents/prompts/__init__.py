"""Versioned prompt templates. Each prompt id@version is recorded on traces and eval reports."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PROMPT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    system: str
    description: str = ""

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


class _Defaults(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@lru_cache(maxsize=32)
def load_prompt(prompt_id: str) -> Prompt:
    raw = yaml.safe_load((PROMPT_DIR / f"{prompt_id}.yaml").read_text(encoding="utf-8"))
    return Prompt(raw["id"], int(raw["version"]), raw["system"], raw.get("description", ""))


def render(prompt_id: str, **values: str) -> tuple[str, str]:
    """Render a prompt with stable values only (property facts, canary) so the prefix stays cacheable."""
    prompt = load_prompt(prompt_id)
    return prompt.system.format_map(_Defaults(values)).strip(), prompt.ref
