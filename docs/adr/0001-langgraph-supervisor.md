# ADR 0001: A LangGraph supervisor graph instead of one big prompt

- Status: accepted
- Date: 2026-11-24

## Context

A customer turn can mix jobs ("book 3 nights from 12 Dec, and what's the cancellation policy?"). Some jobs call
backends, one answers from documents, and some must stop and wait hours for a manager. One prompt with every tool and
every policy in context makes each turn slower and more expensive, makes failures hard to attribute, and has no natural
place to pause for a human.

## Decision

Model the conversation as a LangGraph `StateGraph`:

- `ingest` (guardrail verdict, per-turn reset) → `route` (supervisor) → specialists fanned out with `Send`
  (Booking, Pricing, Policy run in parallel) → `join` → `compose` (output guardrails) → `finalize`.
- Risky actions route to `interim` → `approval_gate`, which calls `interrupt()`. The graph is checkpointed per session
  (thread id = session id), so it resumes after an operator decides, even after a restart.
- Each specialist has its own versioned prompt and its own tool allow-list.

## Consequences

- Every node is a span, so latency, cost and failure have an owner in the trace.
- Specialists get small prompts and few tools, which helps tool-call accuracy and cost.
- The approval pause is a first-class graph state, not a flag scattered through prompt logic.
- More moving parts than a single prompt: state reducers (`Overwrite` to reset per-turn lists) and `Send` payloads
  have to be understood by contributors. `agents/graph.py` documents the flow at the top.

## Alternatives considered

- **Single agent with all tools**: simpler, but no parallelism, larger prompts and an awkward pause/resume.
- **OpenAI Agents SDK / CrewAI handoffs**: viable; LangGraph was chosen for explicit state, checkpointers and
  `interrupt()` semantics, which this project leans on.
