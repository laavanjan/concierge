"""Typed facade over the three microservices, used by the MCP server.

Maps HTTP outcomes to business statuses the agents understand
(ok / approval_required / not_found / unavailable / invalid) and retries
transient failures. POSTs that create things carry an Idempotency-Key so a
retried booking can never double-book.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from services.registry import service_client

log = logging.getLogger(__name__)


class BackendUnavailable(Exception):
    """A backend service could not be reached after retries."""


@dataclass
class BackendResult:
    status: str
    data: dict[str, Any] | list[Any] = field(default_factory=dict)
    message: str = ""

    def to_tool_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.message:
            payload["message"] = self.message
        if isinstance(self.data, list):
            payload["items"] = self.data
        else:
            payload.update(self.data)
        return payload


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (502, 503, 504)


_STATUS_BY_CODE = {403: "approval_required", 404: "not_found", 409: "unavailable", 422: "invalid", 400: "invalid"}


class BackendClient:
    def __init__(self, attempts: int = 3) -> None:
        self.attempts = attempts

    async def _call(self, service: str, method: str, path: str, **kwargs: Any) -> BackendResult:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.attempts),
                wait=wait_exponential(multiplier=0.1, max=1.0),
                retry=retry_if_exception(_transient),
                reraise=True,
            ):
                with attempt:
                    resp = await service_client(service).request(method, path, **kwargs)
                    if resp.status_code in (502, 503, 504):
                        resp.raise_for_status()
        except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            log.warning("backend %s %s %s failed: %s", service, method, path, exc)
            raise BackendUnavailable(f"{service} unavailable: {exc}") from exc
        if resp.status_code < 300:
            return BackendResult("ok", resp.json())
        try:
            detail = resp.json().get("detail", {})
        except ValueError:
            detail = {}
        if isinstance(detail, list):  # FastAPI request validation errors
            message = "; ".join(f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail)
            return BackendResult("invalid", {"error": "validation_error"}, message)
        if not isinstance(detail, dict):
            detail = {"message": str(detail)}
        status = _STATUS_BY_CODE.get(resp.status_code, "error")
        if resp.status_code == 409 and detail.get("error") not in ("unavailable",):
            status = detail.get("error", "conflict")
        if resp.status_code >= 500:
            raise BackendUnavailable(f"{service} error {resp.status_code}")
        return BackendResult(status, {k: v for k, v in detail.items() if k != "message"}, detail.get("message", ""))

    # ---- inventory -------------------------------------------------------
    async def room_types(self) -> BackendResult:
        return await self._call("inventory", "GET", "/room-types")

    async def availability(self, check_in: str, check_out: str, guests: int, rooms: int, room_type: str | None) -> BackendResult:
        params: dict[str, Any] = {"check_in": check_in, "check_out": check_out, "guests": guests, "rooms": rooms}
        if room_type:
            params["room_type"] = room_type
        return await self._call("inventory", "GET", "/availability", params=params)

    # ---- pricing ---------------------------------------------------------
    async def quote(self, **body: Any) -> BackendResult:
        return await self._call("pricing", "POST", "/quotes", json=body)

    async def get_quote(self, quote_id: str) -> BackendResult:
        return await self._call("pricing", "GET", f"/quotes/{quote_id}")

    async def rate_rules(self) -> BackendResult:
        return await self._call("pricing", "GET", "/rate-rules")

    async def override_price(self, quote_id: str, new_total: float, reason: str, approval_token: str | None) -> BackendResult:
        return await self._call(
            "pricing",
            "POST",
            f"/quotes/{quote_id}/override",
            json={"new_total": new_total, "reason": reason, "approval_token": approval_token},
        )

    # ---- reservations ----------------------------------------------------
    async def create_reservation(self, body: dict[str, Any], idempotency_key: str | None) -> BackendResult:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return await self._call("reservations", "POST", "/reservations", json=body, headers=headers)

    async def get_reservation(self, reservation_id: str, email: str) -> BackendResult:
        return await self._call("reservations", "GET", f"/reservations/{reservation_id}", params={"email": email})

    async def cancellation_preview(self, reservation_id: str, email: str) -> BackendResult:
        return await self._call(
            "reservations", "GET", f"/reservations/{reservation_id}/cancellation-preview", params={"email": email}
        )

    async def cancel(self, reservation_id: str, email: str, reason: str, approval_token: str | None) -> BackendResult:
        return await self._call(
            "reservations",
            "POST",
            f"/reservations/{reservation_id}/cancel",
            json={"guest_email": email, "reason": reason, "approval_token": approval_token},
        )

    async def modify(self, reservation_id: str, body: dict[str, Any]) -> BackendResult:
        return await self._call("reservations", "POST", f"/reservations/{reservation_id}/modify", json=body)

    async def goodwill_refund(
        self, reservation_id: str, email: str, amount: float, reason: str, approval_token: str | None
    ) -> BackendResult:
        return await self._call(
            "reservations",
            "POST",
            f"/reservations/{reservation_id}/refunds",
            json={"guest_email": email, "amount": amount, "reason": reason, "approval_token": approval_token},
        )

    async def list_reservations(self, email: str) -> BackendResult:
        return await self._call("reservations", "GET", "/reservations", params={"email": email})
