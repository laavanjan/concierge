# ADR 0007: Cost-aware routing, model tiers and per-turn budgets

- Status: accepted
- Date: 2026-11-24

## Decision

- **Routing is two-stage.** Deterministic rules run on every turn (free; precise for dates and references); a small
  model refines intents when configured: `ROUTER_MODEL=anthropic:claude-haiku-5-5` or a local
  `ollama:qwen2.5:3b`. The rules decision is the fallback if the small model fails. Smalltalk, greetings, handoffs and
  out-of-scope requests never reach a frontier model.
- **Specialists** use `claude-opus-5-5` at `effort=low` (chat latency), with `claude-sonnet-5-5` as fallback and
  server-side refusal fallbacks enabled (`fallbacks="default"`).
- **Prompt caching:** frozen system prompts carry a cache breakpoint; tools are emitted in a stable sorted order; the
  per-turn context (date, slots, profile) goes in a mid-conversation `system` message after the user turn instead of
  in the system prompt, so the cached prefix is reused across turns and sessions.
- **History window:** the last 16 messages, trimmed in blocks of 8 so the cached prefix shifts rarely; collected
  slots and the customer profile carry what older turns said.
- **Budgets:** each turn has a latency budget (5 s) and a cost budget ($0.05). Over cost, the remaining steps run on
  the cheaper model; over latency, the loop stops and hands off rather than keeping the customer waiting.

## Measuring it

`evals/compare.py` prints the before/after table for one optimisation (for example routing on Opus vs Haiku) from two
live eval runs. Those numbers need API credentials, so they are not in the repo yet: see `docs/evaluation.md`.
