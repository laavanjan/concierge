"""CI quality gate: fail the build if a metric misses its target or regresses against the baseline.

    python -m evals.gate evals/reports/ci.json --baseline evals/reports/baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

THRESHOLDS = Path(__file__).resolve().parent / "thresholds.yaml"


def evaluate(metrics: dict, thresholds: dict, baseline: dict | None, allow_missing: bool = False) -> tuple[list[dict], bool]:
    rows, ok = [], True
    max_drop = thresholds.get("max_drop", 0.02)
    for name, rule in thresholds["targets"].items():
        value = metrics.get(name)
        status = "pass"
        target = ""
        if value is None:
            status, target = "missing", "n/a"
        elif "min" in rule:
            target = f">= {rule['min']}"
            status = "pass" if value >= rule["min"] else "FAIL"
        elif "max" in rule:
            target = f"<= {rule['max']}"
            status = "pass" if value <= rule["max"] else "FAIL"
        base = (baseline or {}).get(name)
        if status == "pass" and base is not None and value is not None and "min" in rule and value < base - max_drop:
            status = f"REGRESSION (baseline {base})"
        if status != "pass" and not (status == "missing" and allow_missing):
            ok = False
        rows.append({"metric": name, "value": value, "target": target, "baseline": base, "status": status})
    return rows, ok


def markdown(rows: list[dict]) -> str:
    lines = ["| Metric | Value | Target | Baseline | Status |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['metric']} | {r['value']} | {r['target']} | {r['baseline'] if r['baseline'] is not None else '-'} | {r['status']} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--summary", default=None, help="append the markdown table here (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--allow-missing", action="store_true", help="do not fail on metrics of suites that were not run")
    args = parser.parse_args()
    metrics = json.loads(Path(args.report).read_text())["metrics"]
    baseline = json.loads(Path(args.baseline).read_text())["metrics"] if args.baseline and Path(args.baseline).exists() else None
    rows, ok = evaluate(metrics, yaml.safe_load(THRESHOLDS.read_text()), baseline, args.allow_missing)
    table = markdown(rows)
    print(table)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write("## Concierge eval gate\n\n" + table + "\n")
    print("\nQUALITY GATE:", "PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
