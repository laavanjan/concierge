"""Run evaluation suites and write a JSON report.

    python -m evals.run --suite all --mode offline --out evals/reports/offline-latest.json
    python -m evals.run --suite golden --mode live --limit 20      # needs Claude credentials (costs money)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from evals import harness

SUITES = ("golden", "rag", "redteam", "reliability")


def git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


async def run(suites: list[str], mode: str, limit: int | None) -> dict:
    report: dict = {"run": {"mode": mode, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "git_sha": git_sha()}, "metrics": {}, "suites": {}}
    for suite in suites:
        started = time.perf_counter()
        if suite == "golden":
            result = await harness.run_golden(mode, limit)
        elif suite == "rag":
            result = await harness.run_rag(mode)
        elif suite == "redteam":
            result = await harness.run_redteam(mode)
        else:
            result = await harness.run_reliability(mode)
        result["seconds"] = round(time.perf_counter() - started, 1)
        report["metrics"].update(result["metrics"])
        report["suites"][suite] = result
        print(f"[{suite}] {json.dumps(result['metrics'])}  ({result['seconds']} s, {len(result['failures'])} failures)")
    from agents.prompts import load_prompt
    from common.config import get_settings

    settings = get_settings()
    report["run"]["config"] = {
        "agent_model": settings.agent_model if mode == "live" else "offline (deterministic agents)",
        "fallback_model": settings.agent_fallback_model if mode == "live" else None,
        "router_model": settings.router_model if mode == "live" else "rules",
        "embedding": settings.embedding_provider,
        "reranker": settings.reranker,
        "prompts": {p: load_prompt(p).ref for p in ("router", "booking", "pricing", "policy")},
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Concierge evaluation harness")
    parser.add_argument("--suite", default="all", help="all | " + " | ".join(SUITES) + " (comma-separated)")
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--limit", type=int, default=None, help="limit golden conversations (cost control in live mode)")
    parser.add_argument("--out", default="evals/reports/latest.json")
    args = parser.parse_args()
    os.environ.setdefault("LOG_LEVEL", "ERROR")
    logging.basicConfig(level=logging.ERROR)
    for noisy in ("httpx", "mcp", "agents", "rag", "services", "common", "channels"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    suites = list(SUITES) if args.suite == "all" else [s.strip() for s in args.suite.split(",")]
    report = asyncio.run(run(suites, args.mode, args.limit))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"report written to {out}")


if __name__ == "__main__":
    main()
