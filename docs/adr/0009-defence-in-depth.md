# ADR 0009: Defence in depth against prompt injection

- Status: accepted
- Date: 2026-11-24

No single filter stops prompt injection, so Concierge assumes each layer will sometimes fail:

| Layer | Where | What it does |
|---|---|---|
| Input filter | `guardrails/input_guard.py`, `guardrails/injection.py` | weighted signals (override, role hijack, prompt exfiltration, fake delimiters, tool forgery, authority claims, multilingual), re-scanned on leetspeak, spaced letters and base64 payloads; block at 0.75, restrict at 0.40 |
| Restricted mode | `agents/tools.py` | a suspicious (not blocked) message cannot trigger booking changes, cancellations, refunds or overrides |
| Repeat offenders | `agents/graph.py` | three blocked messages in a session open a security-review handoff |
| PII | `guardrails/pii.py` | card numbers (Luhn-checked), CVV, IBAN, SSN, passports masked before the model, logs and checkpoints |
| Tool allow-lists | `agents/tools.py` | each agent sees and can call only its tools; hidden runtime parameters cannot be set by the model |
| Approval tokens | `common/approvals.py`, services | risky actions need a signed, parameter-bound, single-use operator token (ADR 0003) |
| Document sanitising | `rag/documents.py` | hidden markup removed; sentences that instruct or address an AI are quarantined at ingestion |
| Documents as data | `agents/specialists.py` | retrieved text is sent as `document` blocks; prompts say documents are data |
| Output validation | `guardrails/output_guard.py` | canary and verbatim prompt-leak detection, unknown booking references blocked, foreign emails masked, unverified money flagged |
| Booking privacy | reservations service | lookups need reference + email; a mismatch returns the same 404 as "not found" |

The red-team suite (`evals/datasets/redteam.yaml`, 58 attacks in 10 categories plus 22 benign controls) runs in CI.
