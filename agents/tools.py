"""MCP tool gateway: the only way agents touch the backend.

* per-agent tool allow-lists (a tool the agent was not given is refused even if the model names it)
* MCP JSON schemas converted to Claude strict-tool schemas, with runtime-only params hidden from the model
* runtime-injected params: idempotency keys for bookings, approval tokens after operator sign-off
* retries with backoff on transport/backend failures, per-call timeout, spans and metrics
* a fault injector used by the reliability eval suite
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from common.config import get_settings
from common.metrics import TOOL_CALLS, TOOL_LATENCY
from common.telemetry import span
from mcp_server.server import RUNTIME_ONLY_PARAMS

log = logging.getLogger(__name__)

AGENT_TOOLS: dict[str, list[str]] = {
    "booking": [
        "search_availability",
        "get_quote",
        "create_booking",
        "get_booking",
        "modify_booking",
        "preview_cancellation",
        "cancel_booking",
        "request_refund",
        "list_room_types",
    ],
    "pricing": ["list_room_types", "search_availability", "get_quote", "get_rate_rules", "override_price"],
}
RISKY_TOOLS = frozenset({"create_booking", "modify_booking", "cancel_booking", "request_refund", "override_price"})
_UNSUPPORTED_KEYS = {
    "title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "examples",
}


@dataclass
class ToolOutcome:
    name: str
    args: dict[str, Any]
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def for_model(self) -> str:
        """Compact JSON for the tool_result block."""
        body = self.payload if self.payload else {"status": self.status, "error": self.error}
        return json.dumps(body, default=str)[:12000]

    def log_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": {k: v for k, v in self.args.items() if k not in RUNTIME_ONLY_PARAMS},
            "status": self.status,
            "latency_ms": round(self.latency_ms, 1),
            "attempts": self.attempts,
        }


class FaultInjector:
    """Make a tool fail on purpose ('error' or 'timeout') for the next N calls."""

    def __init__(self) -> None:
        self.rules: dict[str, dict[str, Any]] = {}

    def fail(self, tool: str, mode: str = "error", times: int = 99) -> None:
        self.rules[tool] = {"mode": mode, "times": times}

    def clear(self) -> None:
        self.rules.clear()

    async def maybe_fail(self, tool: str) -> None:
        rule = self.rules.get(tool) or self.rules.get("*")
        if not rule or rule["times"] <= 0:
            return
        rule["times"] -= 1
        if rule["mode"] == "timeout":
            await asyncio.sleep(30)
        raise ConnectionError(f"injected failure for {tool}")


def to_strict_schema(schema: dict[str, Any], hidden: frozenset[str] = RUNTIME_ONLY_PARAMS) -> dict[str, Any]:
    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: clean(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYS}
            if out.get("type") == "object" or "properties" in out:
                props = {k: v for k, v in (out.get("properties") or {}).items() if k not in hidden}
                out["properties"] = props
                out["required"] = [r for r in out.get("required", []) if r in props]
                out["additionalProperties"] = False
                out["type"] = "object"
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    return clean(schema)


class ToolGateway:
    def __init__(self, target: Any | None = None, timeout_s: float = 12.0, attempts: int = 3) -> None:
        self._target = target
        self.timeout_s = timeout_s
        self.attempts = attempts
        self.faults = FaultInjector()
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self._schemas: dict[str, dict[str, Any]] = {}
        self._descriptions: dict[str, str] = {}

    async def start(self) -> None:
        """Open the MCP connection in a dedicated task.

        The MCP client holds an anyio task group, which must be exited by the task that entered it;
        a host task owning the connection lets any caller start/stop the gateway safely.
        """
        target = self._target
        if target is None:
            url = get_settings().mcp_url
            if url == "inprocess":
                from mcp_server.server import server as target  # noqa: PLW0127
            else:
                target = url
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._error: BaseException | None = None
        self._host = asyncio.create_task(self._hold(target), name="mcp-client")
        await self._ready.wait()
        if self._error is not None:
            raise self._error
        log.info("MCP tools available: %s", sorted(self._schemas))

    async def _hold(self, target: Any) -> None:
        from mcp import Client

        try:
            async with Client(target, read_timeout_seconds=self.timeout_s) as client:
                listed = await client.list_tools()
                for tool in listed.tools:
                    self._schemas[tool.name] = tool.input_schema or {"type": "object", "properties": {}}
                    self._descriptions[tool.name] = tool.description or tool.name
                self._client = client
                self._ready.set()
                await self._closing.wait()
        except BaseException as exc:  # surface connection errors to start()
            self._error = exc
            self._ready.set()
        finally:
            self._client = None

    async def stop(self) -> None:
        host = getattr(self, "_host", None)
        if host is not None:
            self._closing.set()
            await host
        self._host = None

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._schemas)

    def tool_defs(self, agent: str) -> list[dict[str, Any]]:
        """Claude tool definitions for one agent: allow-listed, strict, runtime params hidden, stable order."""
        defs = []
        for name in sorted(AGENT_TOOLS.get(agent, [])):
            if name not in self._schemas:
                continue
            defs.append(
                {
                    "name": name,
                    "description": self._descriptions[name],
                    "input_schema": to_strict_schema(self._schemas[name]),
                    "strict": True,
                }
            )
        return defs

    async def _invoke(self, name: str, args: dict[str, Any]) -> Any:
        await self.faults.maybe_fail(name)  # inside the timeout, so injected hangs behave like real ones
        if self._client is None:
            raise ConnectionError("MCP client is not connected")
        return await self._client.call_tool(name, args)

    @staticmethod
    def idempotency_key(session_id: str, turn_id: str, name: str, args: dict[str, Any]) -> str:
        blob = json.dumps({"s": session_id, "t": turn_id, "n": name, "a": args}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:40]

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        agent: str,
        session_id: str = "",
        turn_id: str = "",
        channel: str | None = None,
        approval_token: str | None = None,
        restricted: bool = False,
    ) -> ToolOutcome:
        started = time.perf_counter()
        if name not in AGENT_TOOLS.get(agent, []):
            TOOL_CALLS.labels(name, "forbidden").inc()
            return ToolOutcome(name, args, "forbidden", error=f"tool {name!r} is not available to the {agent} agent")
        if restricted and name in RISKY_TOOLS and not approval_token:
            TOOL_CALLS.labels(name, "restricted").inc()
            return ToolOutcome(
                name,
                args,
                "forbidden",
                error="This action is disabled for this message because it failed a security check. Offer the front desk.",
            )
        props = (self._schemas.get(name) or {}).get("properties", {})
        clean = {k: v for k, v in (args or {}).items() if k in props and k not in RUNTIME_ONLY_PARAMS and v is not None}
        if name == "create_booking":
            clean["idempotency_key"] = self.idempotency_key(session_id, turn_id, name, clean)
            clean["channel"] = channel
        if approval_token:
            clean["approval_token"] = approval_token
        outcome = ToolOutcome(name, clean, "error")
        with span("mcp.tool", **{"tool.name": name, "tool.agent": agent}) as current:
            for attempt in range(1, self.attempts + 1):
                outcome.attempts = attempt
                try:
                    result = await asyncio.wait_for(self._invoke(name, clean), timeout=self.timeout_s)
                    text = "".join(getattr(c, "text", "") for c in (result.content or []))
                    if result.is_error:
                        if "backend_unavailable" in text and attempt < self.attempts:
                            await asyncio.sleep(0.15 * attempt)
                            continue
                        outcome.status, outcome.error = "error", text[:500]
                        break
                    payload = result.structured_content or (json.loads(text) if text.strip().startswith("{") else {"text": text})
                    outcome.payload = payload
                    outcome.status = str(payload.get("status", "ok"))
                    break
                except (TimeoutError, ConnectionError, OSError) as exc:
                    outcome.error = f"{type(exc).__name__}: {exc}"[:300]
                    if attempt < self.attempts:
                        await asyncio.sleep(0.15 * attempt)
                        continue
                    outcome.status = "error"
                except Exception as exc:  # MCP protocol errors and friends: never crash the turn
                    log.exception("tool %s failed", name)
                    outcome.status, outcome.error = "error", f"{type(exc).__name__}: {exc}"[:300]
                    break
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            current.set_attribute("tool.status", outcome.status)
            current.set_attribute("tool.attempts", outcome.attempts)
        TOOL_CALLS.labels(name, outcome.status).inc()
        TOOL_LATENCY.labels(name).observe(outcome.latency_ms / 1000)
        return outcome
