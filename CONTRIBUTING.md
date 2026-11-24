# Contributing

## Setup

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
make test && make eval
make dev                                          # http://localhost:8000
```

No API keys are needed: without `ANTHROPIC_API_KEY` the deterministic agents run.

## Before you open a pull request

- `make lint test eval` must pass. CI runs the same, and the eval gate fails a PR that misses a target or regresses
  more than 2 points against `evals/reports/baseline.json`.
- If you intentionally change behaviour (new prompt version, new routing rule), regenerate the baseline
  (`make eval` then copy `evals/reports/offline-latest.json` to `evals/reports/baseline.json`) and say why in the PR.

## Common changes

| Change | Where | Remember |
|---|---|---|
| New backend capability | service endpoint → `services/client.py` → tool in `mcp_server/server.py` | add it to the right agent in `AGENT_TOOLS`; risky ones go in `RISKY_TOOLS` and should be enforced by the service |
| Prompt change | copy-edit `agents/prompts/<agent>.yaml` and bump `version` | the version is recorded on traces and eval reports |
| New policy document | drop `.md`, `.yaml` (FAQ) or `.pdf` into `data/policies/` | add labelled questions to `evals/datasets/rag_questions.yaml` |
| New attack pattern | `evals/datasets/redteam.yaml` | add a benign look-alike to `benign:` to keep false positives honest |
| New channel | adapter under `channels/` that builds an `InboundMessage` and renders an `OutboundMessage` | register an async sender with `runtime.register_sender` for push updates |
| New domain (car rental, tours, tickets) | `data/domain/*.yaml` + `data/policies/` | the architecture does not change |

## Design decisions

Significant decisions get an ADR in `docs/adr/` (context, decision, consequences, alternatives). Read ADRs 0001-0009
before changing the graph, the tool boundary or the approval flow.

## Style

Python 3.11, `ruff` (config in `pyproject.toml`), type hints throughout, async I/O. Keep secrets out of the repo
(`.env` is git-ignored) and out of logs (`PiiMaskingFilter`).
