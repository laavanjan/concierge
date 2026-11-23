"""Token pricing and per-turn budgets.

Prices are USD per million tokens (first-party Claude API list prices, Nov 2026).
Cache writes bill at 1.25x input, cache reads at 0.1x input (5-minute TTL).
Local models (Ollama / vLLM) are costed at zero marginal API spend.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.00, 40.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


def cost_usd(model: str, usage: Usage) -> float:
    model = model.split(":", 1)[-1]
    if model not in PRICES_PER_MTOK:
        return 0.0
    price_in, price_out = PRICES_PER_MTOK[model]
    total = (
        usage.input_tokens * price_in
        + usage.cache_write_tokens * price_in * 1.25
        + usage.cache_read_tokens * price_in * 0.10
        + usage.output_tokens * price_out
    )
    return round(total / 1_000_000, 6)


@dataclass
class TurnBudget:
    """Latency and cost guard for a single customer turn."""

    latency_ms: int
    cost_usd: float
    started: float = field(default_factory=time.perf_counter)
    spent_usd: float = 0.0
    exceeded: list[str] = field(default_factory=list)

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def charge(self, amount: float) -> None:
        self.spent_usd += amount

    def latency_left_ms(self) -> float:
        return self.latency_ms - self.elapsed_ms()

    def over_latency(self) -> bool:
        over = self.elapsed_ms() > self.latency_ms
        if over and "latency" not in self.exceeded:
            self.exceeded.append("latency")
        return over

    def over_cost(self) -> bool:
        over = self.spent_usd > self.cost_usd
        if over and "cost" not in self.exceeded:
            self.exceeded.append("cost")
        return over
