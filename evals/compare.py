"""Before/after table for one optimisation (e.g. routing on the frontier model vs the small model).

    python -m evals.run --suite golden --mode live --limit 40 --out evals/reports/router-opus.json   # ROUTER_MODEL=anthropic:claude-opus-5-5
    python -m evals.run --suite golden --mode live --limit 40 --out evals/reports/router-haiku.json  # ROUTER_MODEL=anthropic:claude-haiku-5-5
    python -m evals.compare evals/reports/router-opus.json evals/reports/router-haiku.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROWS = [
    ("Task success", "golden.task_success", "pct"),
    ("Tool-call accuracy", "golden.tool_call_accuracy", "pct"),
    ("Routing accuracy", "golden.routing_accuracy", "pct"),
    ("p50 reply latency, web", "latency.web.p50_ms", "ms"),
    ("p95 reply latency, web", "latency.web.p95_ms", "ms"),
    ("Cost per turn", "cost.usd_per_turn", "usd"),
    ("Cost per completed booking", "cost.usd_per_booking", "usd"),
]


def fmt(value, kind: str) -> str:
    if value is None:
        return "-"
    return {"pct": f"{value * 100:.1f}%", "ms": f"{value:,.0f} ms", "usd": f"${value:.4f}"}[kind]


def delta(a, b, kind: str) -> str:
    if a in (None, 0) or b is None:
        return "-"
    change = (b - a) / a * 100
    return f"{change:+.0f}%"


def main() -> None:
    a_path, b_path = Path(sys.argv[1]), Path(sys.argv[2])
    a, b = (json.loads(p.read_text()) for p in (a_path, b_path))
    print(f"| Metric | {a_path.stem} | {b_path.stem} | Change |")
    print("|---|---|---|---|")
    for label, key, kind in ROWS:
        va, vb = a["metrics"].get(key), b["metrics"].get(key)
        print(f"| {label} | {fmt(va, kind)} | {fmt(vb, kind)} | {delta(va, vb, kind)} |")


if __name__ == "__main__":
    main()
