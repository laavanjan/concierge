"""Signed approval tokens for risky actions (refunds, price overrides, large bookings).

An operator approval produces a short-lived HMAC token bound to the exact action
and parameters that were approved. Backend services verify the token themselves
(zero trust): a prompt-injected agent cannot forge one, and a token for a $50
refund on AUR-123456 cannot be replayed for $500 or for another booking.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from common.config import get_settings


class ApprovalError(Exception):
    """The token is missing, malformed, expired, or bound to different params."""


@dataclass(frozen=True)
class ApprovalClaims:
    action: str
    params_hash: str
    approval_id: str
    approved_by: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical_params(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in sorted(params.items()):
        if isinstance(value, float):
            value = round(value, 2)
        out[key] = value
    return out


def params_hash(action: str, params: dict[str, Any]) -> str:
    blob = json.dumps(
        {"action": action, "params": canonical_params(params)}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _sign(body: str) -> str:
    secret = get_settings().approval_secret.encode()
    return _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())


def issue_token(
    action: str, params: dict[str, Any], approval_id: str, approved_by: str, ttl_s: int | None = None
) -> str:
    ttl = ttl_s if ttl_s is not None else get_settings().approval_ttl_s
    payload = {
        "a": action,
        "h": params_hash(action, params),
        "id": approval_id,
        "by": approved_by,
        "exp": int(time.time()) + ttl,
    }
    body = _b64(json.dumps(payload, sort_keys=True).encode())
    return f"{body}.{_sign(body)}"


def verify_token(token: str | None, action: str, params: dict[str, Any]) -> ApprovalClaims:
    if not token:
        raise ApprovalError("missing approval token")
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise ApprovalError("malformed approval token") from exc
    if not hmac.compare_digest(sig, _sign(body)):
        raise ApprovalError("bad approval token signature")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApprovalError("malformed approval token payload") from exc
    if payload.get("a") != action:
        raise ApprovalError("approval token is for a different action")
    if payload.get("h") != params_hash(action, params):
        raise ApprovalError("approval token does not match these parameters")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ApprovalError("approval token expired")
    return ApprovalClaims(
        action=action,
        params_hash=payload["h"],
        approval_id=str(payload["id"]),
        approved_by=str(payload.get("by", "")),
        expires_at=int(payload["exp"]),
    )
