"""Generate the golden conversation set (deterministic, seed 7) -> evals/datasets/golden_conversations.jsonl.

Each conversation has scripted user turns, the backend calls that must happen (name + key arguments, checked in
code), calls that must NOT happen, and the expected end state of the reservation system.

    python -m evals.generate_golden
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent / "datasets" / "golden_conversations.jsonl"
TODAY = date(2026, 11, 24)

ROOMS = {
    "STD": ["the standard queen", "a standard room", "the standard queen room", "the cheapest room"],
    "DLX": ["the deluxe king", "a sea view room", "the deluxe king sea view", "a deluxe room"],
    "FAM": ["the family suite", "a family room", "the family suite please"],
    "PEN": ["the penthouse", "the penthouse suite"],
}
CAPACITY = {"STD": 2, "DLX": 3, "FAM": 4, "PEN": 4}
NAMES = [
    ("Alex Morgan", "alex.morgan"), ("Priya Natarajan", "priya.n"), ("Tomas Herrera", "tomas.h"),
    ("Aisha Bello", "aisha.bello"), ("Kenji Watanabe", "kenji.w"), ("Lena Fischer", "lena.f"),
    ("Samuel Okafor", "sam.okafor"), ("Mei Chen", "mei.chen"), ("Rafael Costa", "rafa.costa"),
    ("Hannah Kowalski", "hannah.k"), ("Omar Haddad", "omar.h"), ("Chloe Dubois", "chloe.d"),
]
POLICY_QS = [
    "what's the cancellation policy?", "can I bring my dog?", "what time is check-in?", "is breakfast included?",
    "do you have parking?", "are kids free?",
]


def fmt(d: date, style: int) -> str:
    return [f"{d.day} {d:%b}", f"{d:%b} {d.day}", d.isoformat(), f"{d.day} {d:%B}"][style % 4]


def stay_phrase(rng: random.Random, ci: date, co: date) -> str:
    nights = (co - ci).days
    options = [
        f"from {fmt(ci, 0)} to {fmt(co, 0)}",
        f"{ci.day}-{co.day} {co:%b}" if ci.month == co.month else f"from {fmt(ci, 1)} to {fmt(co, 1)}",
        f"for {nights} nights from {fmt(ci, rng.randint(0, 3))}",
        f"from {fmt(ci, 2)} to {fmt(co, 2)}",
    ]
    return rng.choice(options)


def party(rng: random.Random, guests: int) -> str:
    choices = {
        1: ["just me", "1 guest", "one person"],
        2: ["2 adults", "2 people", "me and my wife", "two guests"],
        3: ["2 adults and 1 child", "3 people", "three guests"],
        4: ["a family of 4", "2 adults and 2 kids", "4 people"],
    }
    return rng.choice(choices.get(guests, [f"{guests} people"]))


def pen_free(ci: date, co: date) -> bool:
    from common.domain import domain
    from services.inventory.app import background_booked

    room = domain().room("PEN")
    return all(background_booked(room, ci + timedelta(days=i)) == 0 for i in range((co - ci).days))


def random_stay(rng: random.Random, nights: tuple[int, int] = (2, 4), start: int = 10, end: int = 150) -> tuple[date, date]:
    ci = TODAY + timedelta(days=rng.randint(start, end))
    return ci, ci + timedelta(days=rng.randint(*nights))


def person(rng: random.Random) -> tuple[str, str]:
    name, handle = rng.choice(NAMES)
    return name, f"{handle}@example.com"


def quote_args(room: str, ci: date, co: date, guests: int) -> dict:
    return {"room_type": room, "check_in": ci.isoformat(), "check_out": co.isoformat(), "guests": guests}


def reservation(room: str, ci: date, co: date, guests: int, status: str = "confirmed") -> dict:
    return {"room_type": room, "check_in": ci.isoformat(), "check_out": co.isoformat(), "guests": guests, "booking_status": status}


def gen_direct(rng: random.Random, n: int) -> list[dict]:
    out = []
    while len(out) < n:
        room = rng.choice(["STD", "DLX", "DLX", "FAM", "PEN"])
        guests = rng.randint(1, CAPACITY[room]) if room != "STD" else rng.randint(1, 2)
        ci, co = random_stay(rng, (2, 3) if room == "PEN" else (2, 4))  # keep under the large-booking threshold
        if room == "PEN" and not pen_free(ci, co):
            continue
        name, email = person(rng)
        opener = rng.choice(["Book", "I'd like to book", "Can I reserve", "Please book"])
        out.append(
            {
                "category": "new_booking_direct",
                "turns": [
                    f"{opener} {rng.choice(ROOMS[room])} {stay_phrase(rng, ci, co)} for {party(rng, guests)}",
                    rng.choice(["yes please, {name}, {email}", "Yes, book it. {name} {email}", "{name}, {email}"]).format(name=name, email=email),
                ],
                "expected_calls": [{"name": "get_quote", "args": quote_args(room, ci, co, guests)}, {"name": "create_booking", "args": {"guest_email": email}}],
                "forbidden_calls": [],
                "final": {"reservation": reservation(room, ci, co, guests), "approvals": []},
            }
        )
    return out


def gen_choose_room(rng: random.Random, n: int) -> list[dict]:
    out = []
    while len(out) < n:
        guests = rng.randint(1, 4)
        room = rng.choice([r for r, cap in CAPACITY.items() if cap >= guests and r != "PEN"])
        ci, co = random_stay(rng)
        name, email = person(rng)
        turns = [
            rng.choice(["I need a room {stay} for {party}", "Do you have availability {stay} for {party}?", "Hi! {party}, {stay}"]).format(
                stay=stay_phrase(rng, ci, co), party=party(rng, guests)
            ),
            rng.choice(["{room} please", "I'll take {room}", "{room}"]).format(room=rng.choice(ROOMS[room])),
        ]
        if rng.random() < 0.4:
            turns += ["yes", f"{name}, {email}"]
        else:
            turns += [f"yes please. {name}, {email}"]
        out.append(
            {
                "category": "new_booking_choose_room",
                "turns": turns,
                "expected_calls": [
                    {"name": "search_availability", "args": {"check_in": ci.isoformat(), "check_out": co.isoformat(), "guests": guests}},
                    {"name": "get_quote", "args": quote_args(room, ci, co, guests)},
                    {"name": "create_booking", "args": {"guest_email": email}},
                ],
                "forbidden_calls": [],
                "final": {"reservation": reservation(room, ci, co, guests), "approvals": []},
            }
        )
    return out


def gen_compound(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci, co = random_stay(rng, (2, 3))
        guests = 2
        name, email = person(rng)
        question = rng.choice(POLICY_QS)
        out.append(
            {
                "category": "compound_booking_policy",
                "turns": [f"book a {(co - ci).days}-night stay for 2 from {fmt(ci, 0)}, {question}", "the deluxe king", f"yes, {name}, {email}"],
                "expected_calls": [{"name": "get_quote", "args": quote_args("DLX", ci, co, guests)}, {"name": "create_booking", "args": {"guest_email": email}}],
                "forbidden_calls": [],
                "expect_first_turn_agents": ["booking", "policy"],
                "final": {"reservation": reservation("DLX", ci, co, guests), "approvals": []},
            }
        )
    return out


def gen_modify(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci, co = random_stay(rng, (2, 3), 20, 120)
        shift = rng.choice([-3, -2, 1, 2, 5, 7])
        new_ci = ci + timedelta(days=shift)
        new_co = new_ci + (co - ci)
        name, email = person(rng)
        out.append(
            {
                "category": "modify_booking",
                "setup": [{"room_type": "DLX", "check_in": ci.isoformat(), "check_out": co.isoformat(), "guests": 2, "name": name, "email": email}],
                "turns": [
                    rng.choice(["please move {rid0} to {d}, my email is {email}", "Can I change booking {rid0} to start {d}? {email}",
                                "reschedule {rid0} to {d} please ({email})"]).replace("{d}", fmt(new_ci, rng.randint(0, 3))).replace("{email}", email),
                    "yes",
                ],
                "expected_calls": [{"name": "modify_booking", "args": {"check_in": new_ci.isoformat(), "check_out": new_co.isoformat()}}],
                "forbidden_calls": ["cancel_booking", "create_booking"],
                "final": {"reservation": reservation("DLX", new_ci, new_co, 2), "approvals": []},
            }
        )
    return out


def gen_cancel(rng: random.Random, n: int, large: bool) -> list[dict]:
    out = []
    while len(out) < n:
        if large:
            ci, co = random_stay(rng, (2, 3), 20, 120)
            room = "PEN"
            if not pen_free(ci, co):
                continue
        else:
            ci, co = random_stay(rng, (2, 3), 20, 120)
            room = rng.choice(["STD", "DLX"])
        name, email = person(rng)
        out.append(
            {
                "category": "cancel_with_approval" if large else "cancel_booking",
                "setup": [{"room_type": room, "check_in": ci.isoformat(), "check_out": co.isoformat(), "guests": 2, "name": name, "email": email}],
                "turns": [
                    rng.choice(["I want to cancel my booking {rid0}, email {email}", "please cancel {rid0} ({email})",
                                "I need to cancel reservation {rid0}. My email is {email}"]).replace("{email}", email),
                    rng.choice(["yes", "yes please cancel it", "go ahead"]),
                ],
                "expected_calls": [
                    {"name": "preview_cancellation", "args": {"guest_email": email}},
                    {"name": "cancel_booking", "args": {"guest_email": email}},
                ],
                "forbidden_calls": ["create_booking"],
                "final": {
                    "reservation": reservation(room, ci, co, 2, "confirmed" if large else "cancelled"),
                    "approvals": ["cancel_with_refund"] if large else [],
                },
            }
        )
    return out


def gen_large_booking(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci, co = random_stay(rng, (6, 8), 10, 100)
        name, email = person(rng)
        out.append(
            {
                "category": "large_booking_approval",
                "turns": [f"book 2 family suites {stay_phrase(rng, ci, co)} for 8 people", f"yes, {name}, {email}"],
                "expected_calls": [{"name": "create_booking", "args": {"guest_email": email}}],
                "forbidden_calls": [],
                "final": {"reservation": None, "approvals": ["create_large_booking"]},
            }
        )
    return out


def gen_refund(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci, co = random_stay(rng, (2, 3), 20, 120)
        name, email = person(rng)
        amount = rng.choice([25, 40, 60, 80])
        reason = rng.choice(["the room was noisy", "the shower was cold", "the wifi was down all night"])
        out.append(
            {
                "category": "goodwill_refund_approval",
                "setup": [{"room_type": "STD", "check_in": ci.isoformat(), "check_out": co.isoformat(), "guests": 2, "name": name, "email": email}],
                "turns": [f"I'd like a refund of ${amount} on {{rid0}} because {reason}. My email is {email}"],
                "expected_calls": [{"name": "request_refund", "args": {"amount": amount, "guest_email": email}}],
                "forbidden_calls": ["cancel_booking"],
                "final": {"reservation": reservation("STD", ci, co, 2), "approvals": ["goodwill_refund"]},
            }
        )
    return out


def gen_decline(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci, co = random_stay(rng)
        out.append(
            {
                "category": "declined_quote",
                "turns": [f"book the deluxe king {stay_phrase(rng, ci, co)} for 2 adults", rng.choice(["no thanks", "no, that's too expensive", "nope"])],
                "expected_calls": [{"name": "get_quote", "args": quote_args("DLX", ci, co, 2)}],
                "forbidden_calls": ["create_booking"],
                "final": {"reservation": None, "approvals": []},
            }
        )
    return out


def gen_promo(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci = date(2026, 12, 1) + timedelta(days=rng.randint(0, 80))
        co = ci + timedelta(days=rng.randint(2, 4))
        name, email = person(rng)
        out.append(
            {
                "category": "promo_code",
                "turns": [f"book the standard queen {stay_phrase(rng, ci, co)} for 2 people with promo code WINTER10", f"yes, {name}, {email}"],
                "expected_calls": [{"name": "get_quote", "args": {**quote_args("STD", ci, co, 2), "promo_code": "WINTER10"}},
                                   {"name": "create_booking", "args": {"guest_email": email}}],
                "forbidden_calls": [],
                "final": {"reservation": reservation("STD", ci, co, 2), "approvals": [], "promo_applied": "WINTER10"},
            }
        )
    return out


def gen_pricing_then_book(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci, co = random_stay(rng, (2, 4))
        name, email = person(rng)
        out.append(
            {
                "category": "pricing_then_book",
                "turns": [f"how much is the family suite {stay_phrase(rng, ci, co)} for 4 people?", f"great, book it. {name}, {email}"],
                "expected_calls": [{"name": "get_quote", "args": quote_args("FAM", ci, co, 4)}, {"name": "create_booking", "args": {"guest_email": email}}],
                "forbidden_calls": [],
                "expect_first_turn_agents": ["pricing"],
                "final": {"reservation": reservation("FAM", ci, co, 4), "approvals": []},
            }
        )
    return out


def gen_lookup(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci, co = random_stay(rng, (2, 3), 20, 120)
        name, email = person(rng)
        out.append(
            {
                "category": "lookup_booking",
                "setup": [{"room_type": "DLX", "check_in": ci.isoformat(), "check_out": co.isoformat(), "guests": 2, "name": name, "email": email}],
                "turns": [f"can you look up my booking {{rid0}}? email {email}"],
                "expected_calls": [{"name": "get_booking", "args": {"guest_email": email}}],
                "forbidden_calls": ["cancel_booking", "modify_booking", "create_booking"],
                "final": {"reservation": reservation("DLX", ci, co, 2), "approvals": []},
            }
        )
    return out


def gen_capacity(rng: random.Random, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        ci, co = random_stay(rng)
        out.append(
            {
                "category": "over_capacity",
                "turns": [f"book the standard queen {stay_phrase(rng, ci, co)} for 5 people"],
                "expected_calls": [],
                "forbidden_calls": ["create_booking"],
                "final": {"reservation": None, "approvals": []},
            }
        )
    return out


def main() -> None:
    rng = random.Random(7)
    convs: list[dict] = []
    convs += gen_direct(rng, 24)
    convs += gen_choose_room(rng, 20)
    convs += gen_compound(rng, 10)
    convs += gen_modify(rng, 10)
    convs += gen_cancel(rng, 10, large=False)
    convs += gen_cancel(rng, 5, large=True)
    convs += gen_large_booking(rng, 5)
    convs += gen_refund(rng, 5)
    convs += gen_decline(rng, 5)
    convs += gen_promo(rng, 5)
    convs += gen_pricing_then_book(rng, 5)
    convs += gen_lookup(rng, 4)
    convs += gen_capacity(rng, 3)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        for i, conv in enumerate(convs, start=1):
            fh.write(json.dumps({"id": f"g{i:03d}", "channel": rng.choice(["web", "web", "whatsapp", "email"]), **conv}) + "\n")
    print(f"wrote {len(convs)} conversations to {OUT}")


if __name__ == "__main__":
    main()
