# ADR 0005: Deterministic agents as the offline mode and the last fallback

- Status: accepted
- Date: 2026-11-24

## Context

CI needs reproducible evaluation without paying for tokens, contributors need to run the stack without API keys, and
production needs a plan for model outages better than "sorry, try later".

## Decision

Implement each specialist twice over the **same MCP tools and the same graph**:

1. the LLM agent (Claude tool-use loop, versioned prompt), and
2. a deterministic agent (regex NLU + a slot-filling state machine) in `agents/offline.py`.

At runtime each specialist tries: frontier model → fallback model → deterministic agent → operator handoff. The LLM
path keeps the deterministic agent's state (`flow.offer`, `slots`) up to date from tool results, so a mid-conversation
outage continues the booking instead of starting over.

## Consequences

- `make eval` runs the whole system (routing, tools, approvals, guardrails, channels) for free and deterministically;
  the live suite (`--mode live`) measures model quality on the same datasets.
- The reliability suite injects tool, retrieval and model failures and requires 100% graceful outcomes.
- Two implementations to keep consistent. The golden set and the LLM-path tests (scripted model) guard both.
- Offline numbers measure the plumbing, not the model. The README reports them separately from live numbers.
