# ADR 0006: Session state in Redis, long-term memory in Postgres

- Status: accepted
- Date: 2026-11-24

## Decision

| Data | Store | Why |
|---|---|---|
| Conversation state, pending interrupts | LangGraph checkpointer: Redis (`AsyncRedisSaver`, sliding TTL) in production; SQLite locally; in-memory in tests | resume after approvals and restarts; expires with the session |
| Session index, outbox for async messages | Redis (`RedisSessionStore`) | cheap, TTL'd, shared by gateway replicas |
| Customer identities across channels, preferences | Postgres (`customers`, `identities`, `customer_facts`) | durable, queryable, survives session expiry |
| Approvals and handoff tickets | Postgres | audit trail |
| Domain events | Redis Streams (consumer groups) | at-least-once fan-out to the worker |
| Each service's data | its own Postgres database | database per service |

- Session ids for phone and email channels are derived from the contact (`sha256(address)`), so a WhatsApp
  conversation continues after a restart even without Redis.
- Giving an email in chat links the web visitor to an existing customer profile (identity merge), so a guest who
  booked on WhatsApp is recognised on the web.

## Consequences

- Stateless gateway replicas; ordering within a session is enforced by a per-session lock in-process (multi-replica
  deployments need sticky routing or a Redis lock; noted in `deploy/k3s/README.md`).
- The Redis checkpointer needs Redis 8 (or Redis Stack) for its JSON/Search modules.
