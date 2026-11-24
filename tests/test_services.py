"""Backend microservices: pricing rules, atomic holds, idempotency, ownership and approval enforcement."""

from __future__ import annotations

from datetime import UTC, date, datetime

from common.approvals import issue_token
from common.domain import domain
from services.client import BackendClient
from services.pricing.engine import price_stay
from services.reservations.policy import cancellation_terms


def test_price_engine_applies_weekend_season_los_promo_and_tax():
    pack = domain()
    room = pack.room("DLX")
    quote = price_stay(pack, room, date(2026, 12, 18), date(2026, 12, 25), 2, 1, {}, "WINTER10")
    nights = {n["date"]: {a["rule"] for a in n["adjustments"]} for n in quote["nightly"]}
    assert "weekend" in nights["2026-12-18"]  # Friday
    assert "season" in nights["2026-12-20"]  # festive peak starts
    assert "season" not in nights["2026-12-19"]
    rules = [d["rule"] for d in quote["discounts"]]
    assert rules == ["length_of_stay", "promo"]  # 7 nights -> -12%, then WINTER10
    taxable = quote["subtotal"] + sum(d["amount"] for d in quote["discounts"])
    assert abs(quote["tax"] - round(taxable * 0.10, 2)) < 0.01
    assert abs(quote["total"] - round(taxable + quote["tax"], 2)) < 0.01


def test_promo_validation_messages():
    pack = domain()
    room = pack.room("STD")
    expired = price_stay(pack, room, date(2027, 3, 10), date(2027, 3, 12), 2, 1, {}, "WINTER10")
    assert expired["promo"]["status"] == "expired"
    too_short = price_stay(pack, room, date(2027, 4, 10), date(2027, 4, 12), 2, 1, {}, "STAYLONG")
    assert too_short["promo"]["status"] == "not_applicable"
    bogus = price_stay(pack, room, date(2027, 4, 10), date(2027, 4, 12), 2, 1, {}, "FREEROOM")
    assert bogus["promo"]["status"] == "invalid"


def test_cancellation_terms_free_late_noshow_and_peak():
    pack = domain()
    nightly = [{"amount": 200.0}, {"amount": 200.0}]
    tz = UTC
    free = cancellation_terms(pack, date(2027, 4, 10), date(2027, 4, 12), 440.0, nightly, datetime(2027, 4, 1, tzinfo=tz))
    assert free["rule"] == "free_cancellation" and free["refund_amount"] == 440.0
    late = cancellation_terms(pack, date(2027, 4, 10), date(2027, 4, 12), 440.0, nightly, datetime(2027, 4, 9, 12, tzinfo=tz))
    assert late["rule"] == "late_cancellation" and late["refund_amount"] == 220.0  # first night + 10% tax charged
    no_show = cancellation_terms(pack, date(2027, 4, 10), date(2027, 4, 12), 440.0, nightly, datetime(2027, 4, 10, 18, tzinfo=tz))
    assert no_show["refund_amount"] == 0.0
    peak = cancellation_terms(pack, date(2026, 12, 24), date(2026, 12, 26), 440.0, nightly, datetime(2026, 12, 20, tzinfo=tz))
    assert peak["peak_season"] and peak["rule"] == "late_cancellation"  # 7-day window in festive peak


async def test_booking_flow_idempotency_and_ownership():
    c = BackendClient()
    quote = await c.quote(room_type="DLX", check_in="2026-12-12", check_out="2026-12-15", guests=2, rooms=1)
    assert quote.status == "ok" and quote.data["quote_id"].startswith("Q-")
    body = {"quote_id": quote.data["quote_id"], "guest_name": "Alex Morgan", "guest_email": "alex@example.com"}
    first = await c.create_reservation(body, "idem-1")
    again = await c.create_reservation(body, "idem-1")
    assert first.status == "ok" and again.data["reservation_id"] == first.data["reservation_id"]
    rid = first.data["reservation_id"]
    assert (await c.get_reservation(rid, "alex@example.com")).status == "ok"
    assert (await c.get_reservation(rid, "mallory@example.com")).status == "not_found"


async def test_holds_are_atomic_and_sell_out():
    c = BackendClient()
    for i in range(10):
        q = await c.quote(room_type="PEN", check_in="2027-04-06", check_out="2027-04-07", guests=2, rooms=1)
        r = await c.create_reservation({"quote_id": q.data["quote_id"], "guest_name": "Guest Number", "guest_email": f"g{i}@example.com"}, None)
        if r.status != "ok":
            assert r.status == "unavailable"
            break
    else:  # pragma: no cover - the penthouse has a single unit
        raise AssertionError("penthouse never sold out")


async def test_large_booking_and_refund_need_bound_single_use_approval():
    c = BackendClient()
    q = await c.quote(room_type="FAM", check_in="2026-12-01", check_out="2026-12-08", guests=8, rooms=2)
    body = {"quote_id": q.data["quote_id"], "guest_name": "Sam Lee", "guest_email": "sam@example.com"}
    blocked = await c.create_reservation(body, None)
    assert blocked.status == "approval_required"
    assert blocked.data["bound_params"] == {"quote_id": q.data["quote_id"], "guest_email": "sam@example.com"}
    wrong = issue_token("create_large_booking", {"quote_id": "Q-OTHER", "guest_email": "sam@example.com"}, "A1", "mgr")
    assert (await c.create_reservation({**body, "approval_token": wrong}, None)).status == "approval_required"
    token = issue_token("create_large_booking", blocked.data["bound_params"], "A2", "mgr")
    booked = await c.create_reservation({**body, "approval_token": token}, None)
    assert booked.status == "ok"
    rid = booked.data["reservation_id"]
    refund_token = issue_token("goodwill_refund", {"reservation_id": rid, "amount": 100.0}, "A3", "mgr")
    assert (await c.goodwill_refund(rid, "sam@example.com", 100, "noise", None)).status == "approval_required"
    assert (await c.goodwill_refund(rid, "sam@example.com", 100, "noise", refund_token)).status == "ok"
    replay = await c.goodwill_refund(rid, "sam@example.com", 100, "noise", refund_token)
    assert replay.status == "approval_already_used"
    tampered = await c.goodwill_refund(rid, "sam@example.com", 500, "noise", refund_token)
    assert tampered.status == "approval_required"


async def test_capacity_and_date_validation():
    c = BackendClient()
    over = await c.quote(room_type="STD", check_in="2026-12-12", check_out="2026-12-15", guests=5, rooms=1)
    assert over.status == "invalid" and "sleeps 2" in over.message
    past = await c.availability("2026-01-01", "2026-01-03", 2, 1, None)
    assert past.status == "invalid"
