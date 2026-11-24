"""Deterministic NLU: the zero-cost router and slot extractor."""

from __future__ import annotations

from datetime import date

import pytest

from agents.nlu import extract_dates, extract_party, rule_decision

T = date(2026, 11, 24)


@pytest.mark.parametrize(
    "text,check_in,check_out,nights",
    [
        ("book a 3-night stay for 2 from 12 Dec", date(2026, 12, 12), None, 3),
        ("12-15 December please", date(2026, 12, 12), date(2026, 12, 15), None),
        ("Dec 20 to Dec 23", date(2026, 12, 20), date(2026, 12, 23), None),
        ("from 2026-12-28 to 2027-01-02", date(2026, 12, 28), date(2027, 1, 2), None),
        ("arriving 3rd March for a week", date(2027, 3, 3), None, 7),
        ("30 Dec - 2 Jan", date(2026, 12, 30), date(2027, 1, 2), None),
        ("tomorrow for two nights", date(2026, 11, 25), None, 2),
        ("this weekend", date(2026, 11, 27), None, 2),
    ],
)
def test_dates(text, check_in, check_out, nights):
    assert extract_dates(text, T) == (check_in, check_out, nights)


@pytest.mark.parametrize(
    "text,guests,rooms",
    [
        ("for 2 adults and 1 child", 3, None),
        ("family of 4", 4, None),
        ("me and my wife", 2, None),
        ("3-night stay for 2 from 12 Dec", 2, None),
        ("two rooms for 6 people", 6, 2),
        ("for 2 nights", None, None),
    ],
)
def test_party(text, guests, rooms):
    assert extract_party(text) == (guests, rooms)


@pytest.mark.parametrize(
    "text,intents,action",
    [
        ("book a 3-night stay for 2 from 12 Dec, what's the cancellation policy?", ["booking", "policy"], "new"),
        ("can I bring my dog?", ["policy"], "none"),
        ("how much is parking?", ["policy"], "none"),
        ("how much is a deluxe king for 12-15 Dec for 2?", ["pricing"], "none"),
        ("I want to cancel my booking AUR-7K3Q9P", ["booking"], "cancel"),
        ("move my reservation AUR-7K3Q9P to 14 Dec", ["booking"], "modify"),
        ("I'd like a refund of $80, the room was noisy", ["booking"], "refund"),
        ("can I speak to a human?", ["handoff"], "none"),
        ("hello", ["smalltalk"], "none"),
        ("what's the weather tomorrow?", ["out_of_scope"], "none"),
    ],
)
def test_rule_router(text, intents, action):
    decision = rule_decision(text, T)
    assert decision.intents == intents and decision.booking_action == action


def test_yes_continues_pending_flow():
    flow = {"agent": "booking", "action": "cancel", "awaiting": "confirm_cancel"}
    decision = rule_decision("yes please", T, flow)
    assert decision.intents == ["booking"] and decision.booking_action == "cancel" and decision.confirm is True


def test_room_alias_needs_word_boundaries():
    assert rule_decision("I want to check my booking", T).slots.room_type is None
    assert rule_decision("the sea view room", T).slots.room_type == "DLX"
