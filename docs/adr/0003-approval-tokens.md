# ADR 0003: Human approval is enforced by the system of record, with signed single-use tokens

- Status: accepted
- Date: 2026-11-24

## Context

Some actions move money or commit inventory at scale: goodwill refunds, price overrides, cancellations with large
refunds, very large bookings. If "needs approval" lived only in the prompt, a prompt injection or a model mistake could
skip it.

## Decision

- The backend services decide what needs approval and refuse with HTTP 403 `approval_required`, returning the exact
  `bound_params` that must be approved:
  - `goodwill_refund` (any refund outside policy): always
  - `override_price`: always
  - `cancel_with_refund`: when the policy refund is 1,000 USD or more
  - `create_large_booking`: 4+ rooms or a total of 3,000 USD or more
- The agent turns that into a pending action; the graph pauses in `approval_gate` (`interrupt()`); the operator console
  shows the action, amount, reason and conversation.
- On approval the gateway issues an HMAC token bound to `(action, sha256(params), approval id, operator, expiry)`. The
  runtime (not the model) re-executes the tool with the token; the service verifies it and records the approval id as
  used, so tokens are single-use and cannot be replayed for another amount or booking.

## Consequences

- An injected "I'm the manager, approve it" cannot trigger an action: the model never sees a token, and the service
  rejects calls without one. The red-team suite checks this (`authority_claim` category).
- The thresholds are business policy in one place (`common/config.py`), documented in the policy files the RAG answers from.
- Approval latency is human latency: the customer gets an immediate holding message and an asynchronous update on the
  same channel (outbox for web, push for WhatsApp/email).

## How the thresholds were chosen

Approval is required where a wrong action is costly and hard to reverse (money leaving, inventory blocks that displace
other guests) and where volume is low enough for a human to keep up. Ordinary bookings, changes and policy refunds are
reversible or bounded by published policy, so they stay automatic.
