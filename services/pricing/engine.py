"""Explainable rate engine. Every adjustment is a line item the Pricing agent can cite."""

from __future__ import annotations

from datetime import date

from common.clock import nights_between
from common.domain import DomainPack, RoomType

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _money(value: float) -> float:
    return round(value + 1e-9, 2)


def nightly_adjustments(pack: DomainPack, night: date, occupancy: float) -> list[dict]:
    rules = pack.rate_rules
    adjustments: list[dict] = []
    if WEEKDAYS[night.weekday()] in rules.weekend.nights:
        adjustments.append({"rule": "weekend", "label": "Weekend night", "pct": rules.weekend.pct})
    for season in rules.seasons:
        if season.contains(night):
            adjustments.append({"rule": "season", "label": season.name, "pct": season.pct})
    for tier in sorted(rules.occupancy, key=lambda t: t.min, reverse=True):
        if occupancy >= tier.min:
            adjustments.append(
                {"rule": "demand", "label": f"High demand ({round(occupancy * 100)}% booked)", "pct": tier.pct}
            )
            break
    return adjustments


def evaluate_promo(pack: DomainPack, code: str | None, check_in: date, nights: int) -> dict | None:
    if not code:
        return None
    normalized = code.strip().upper()
    promo = pack.rate_rules.promo_codes.get(normalized)
    if promo is None:
        return {"code": normalized, "status": "invalid", "message": f"Promo code {normalized} is not recognised."}
    if promo.valid_from and check_in < promo.valid_from:
        return {"code": normalized, "status": "not_applicable", "message": f"{normalized} applies from {promo.valid_from}."}
    if promo.valid_to and check_in > promo.valid_to:
        return {"code": normalized, "status": "expired", "message": f"{normalized} expired on {promo.valid_to}."}
    if promo.min_nights and nights < promo.min_nights:
        return {
            "code": normalized,
            "status": "not_applicable",
            "message": f"{normalized} needs a stay of at least {promo.min_nights} nights.",
        }
    return {"code": normalized, "status": "applied", "pct": promo.pct, "message": promo.description}


def price_stay(
    pack: DomainPack,
    room: RoomType,
    check_in: date,
    check_out: date,
    guests: int,
    rooms: int,
    occupancy: dict[str, float],
    promo_code: str | None = None,
) -> dict:
    nights = nights_between(check_in, check_out)
    rules = pack.rate_rules
    extra_guests = max(0, guests - room.standard_occupancy * rooms)
    nightly = []
    subtotal = 0.0
    for night in nights:
        occ = occupancy.get(night.isoformat(), 0.0)
        adjustments = nightly_adjustments(pack, night, occ)
        pct = sum(a["pct"] for a in adjustments)
        rate = _money(room.base_rate * (1 + pct / 100))
        extra_fee = _money(extra_guests * rules.extra_guest_fee_per_night)
        amount = _money(rate * rooms + extra_fee)
        subtotal += amount
        nightly.append(
            {
                "date": night.isoformat(),
                "base_rate": room.base_rate,
                "adjustments": adjustments,
                "rate": rate,
                "rooms": rooms,
                "extra_guest_fee": extra_fee,
                "amount": amount,
            }
        )
    subtotal = _money(subtotal)

    discounts: list[dict] = []
    for rule in sorted(rules.length_of_stay, key=lambda r: r.min_nights, reverse=True):
        if len(nights) >= rule.min_nights:
            discounts.append(
                {
                    "rule": "length_of_stay",
                    "label": f"{rule.min_nights}+ night stay",
                    "pct": rule.pct,
                    "amount": _money(subtotal * rule.pct / 100),
                }
            )
            break
    promo = evaluate_promo(pack, promo_code, check_in, len(nights))
    if promo and promo["status"] == "applied":
        base = subtotal + sum(d["amount"] for d in discounts)
        discounts.append(
            {
                "rule": "promo",
                "label": f"Promo {promo['code']}",
                "pct": promo["pct"],
                "amount": _money(base * promo["pct"] / 100),
            }
        )
    taxable = _money(subtotal + sum(d["amount"] for d in discounts))
    tax = _money(taxable * pack.property.tax_rate)
    return {
        "room_type": room.code,
        "room_name": room.name,
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "nights": len(nights),
        "guests": guests,
        "rooms": rooms,
        "currency": pack.property.currency,
        "nightly": nightly,
        "subtotal": subtotal,
        "discounts": discounts,
        "promo": promo,
        "tax_rate": pack.property.tax_rate,
        "tax": tax,
        "total": _money(taxable + tax),
    }


def rules_summary(pack: DomainPack) -> list[str]:
    rules = pack.rate_rules
    lines = ["Base nightly rates: " + ", ".join(f"{r.name} ${r.base_rate:.0f}" for r in pack.room_types)]
    lines.append(f"Weekend nights ({', '.join(rules.weekend.nights)}): {rules.weekend.pct:+.0f}%")
    lines += [f"{s.name} ({s.start} to {s.end}): {s.pct:+.0f}%" for s in rules.seasons]
    lines += [f"Demand pricing when a room type is {round(t.min * 100)}%+ booked: {t.pct:+.0f}%" for t in rules.occupancy]
    lines += [f"Stays of {r.min_nights}+ nights: {r.pct:+.0f}%" for r in rules.length_of_stay]
    lines.append(f"Extra guest beyond standard occupancy: ${rules.extra_guest_fee_per_night:.0f} per night")
    lines.append(f"Taxes: {pack.property.tax_rate * 100:.0f}% on the discounted subtotal")
    lines += [f"Promo {code}: {p.description}" for code, p in rules.promo_codes.items()]
    return lines
