# ADR 0008: Stream progress, not raw tokens, to customers

- Status: accepted
- Date: 2026-11-24

## Context

Streaming model tokens straight to the customer gives the best time-to-first-token, but the output guardrails
(hallucinated prices, unknown booking references, prompt leaks, other people's emails) need the whole reply before it
is shown. A streamed claim cannot be un-sent.

## Decision

- The web channel streams **progress events** over SSE as the graph runs (`routing`, `booking agent working`,
  `booking → get_quote (ok)`, `checking the answer`), using LangGraph custom stream events.
- The reply is sent once the output guardrails have passed it.
- We measure both full-reply latency and time-to-first-event per channel (`/ops`, Prometheus).

## Consequences

- Customers see activity within ~100 ms, and never see a reply that the guardrails would have withdrawn.
- WhatsApp, email and voice are not token-streamed anyway, so one policy covers every channel.
- If time-to-first-token becomes the bottleneck, sentence-level streaming with a per-sentence check is the next step.
