"""Cancellation and modification rules. The policy documents in data/policies/ describe the same numbers."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from common.clock import check_in_datetime, nights_between
from common.domain import DomainPack


def is_peak_stay(pack: DomainPack, check_in: date, check_out: date) -> bool:
    peaks = [s for s in pack.rate_rules.seasons if s.pct > 0]
    return any(s.contains(n) for s in peaks for n in nights_between(check_in, check_out))


def free_cancellation_deadline(pack: DomainPack, check_in: date, check_out: date) -> tuple[datetime, bool]:
    peak = is_peak_stay(pack, check_in, check_out)
    window = (
        timedelta(days=pack.policies.peak_free_cancellation_days)
        if peak
        else timedelta(hours=pack.policies.free_cancellation_hours)
    )
    return check_in_datetime(check_in) - window, peak


def cancellation_terms(
    pack: DomainPack,
    check_in: date,
    check_out: date,
    paid: float,
    nightly: list[dict],
    now: datetime,
) -> dict:
    deadline, peak = free_cancellation_deadline(pack, check_in, check_out)
    arrival = check_in_datetime(check_in)
    window_text = (
        f"{pack.policies.peak_free_cancellation_days} days" if peak else f"{pack.policies.free_cancellation_hours} hours"
    )
    if now <= deadline:
        rule, refund = "free_cancellation", paid
        explanation = f"Cancelled more than {window_text} before arrival: full refund."
    elif now < arrival:
        charge_nights = nightly[: pack.policies.late_cancellation_charge_nights]
        charge = round(sum(n["amount"] for n in charge_nights) * (1 + pack.property.tax_rate), 2)
        refund = max(0.0, round(paid - charge, 2))
        rule = "late_cancellation"
        explanation = (
            f"Cancelled within {window_text} of arrival: the first night "
            f"({charge:.2f} incl. tax) is charged and the rest is refunded."
        )
    else:
        rule, refund = "no_show", 0.0
        explanation = "Cancelled after the check-in time: treated as a no-show, no refund."
    return {
        "rule": rule,
        "peak_season": peak,
        "free_cancellation_until": deadline.isoformat(),
        "refund_amount": round(refund, 2),
        "charge": round(paid - refund, 2),
        "explanation": explanation,
    }


def modification_allowed(pack: DomainPack, check_in: date, now: datetime) -> bool:
    return now <= check_in_datetime(check_in) - timedelta(hours=pack.policies.modification_cutoff_hours)
