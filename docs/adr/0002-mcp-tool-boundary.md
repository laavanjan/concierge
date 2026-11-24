# ADR 0002: MCP is the only way agents reach the backend

- Status: accepted
- Date: 2026-11-24

## Context

Agents need inventory, pricing and reservation operations. Calling the microservices directly from agent code couples
prompts to HTTP details and makes the tool surface invisible to other clients.

## Decision

- Expose 11 backend operations as tools on an MCP server (official Python SDK v2, `MCPServer`): `search_availability`,
  `get_quote`, `create_booking`, `get_booking`, `modify_booking`, `preview_cancellation`, `cancel_booking`,
  `request_refund`, `get_rate_rules`, `override_price`, `list_room_types`, plus the policy library as MCP resources.
- The agent runtime is an MCP client (`agents/tools.py`). It converts MCP JSON schemas to Claude strict-tool schemas,
  applies a per-agent allow-list, hides runtime-only parameters (`approval_token`, `idempotency_key`, `channel`) from
  the model and injects them itself, retries transient failures and enforces a timeout.
- Transports: in-process for dev/tests, Streamable HTTP (stateless) in compose/k8s, stdio for Claude Desktop/Code.

## Consequences

- The same tools work from Claude Desktop or Claude Code (`python -m mcp_server --transport stdio`).
- Tool results are structured (`status: ok | approval_required | not_found | unavailable | invalid`), so agents and the
  deterministic fallback reason about business outcomes rather than HTTP codes.
- Idempotency keys derived from (session, turn, args) mean a retried or duplicated `create_booking` cannot double-book.
- One extra hop per tool call (negligible in-process; ~1 ms over the cluster network).
