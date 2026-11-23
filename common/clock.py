"""Single source of 'now' so demos and evals can freeze time (CONCIERGE_TODAY)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from common.config import get_settings


def property_tz() -> tzinfo:
    from common.domain import domain

    name = domain().property.timezone
    return UTC if name.upper() == "UTC" else ZoneInfo(name)


def now() -> datetime:
    frozen = get_settings().concierge_today
    if frozen:
        return datetime.combine(frozen, time(10, 0), tzinfo=property_tz())
    return datetime.now(tz=property_tz())


def today() -> date:
    return now().date()


def utcnow() -> datetime:
    return now().astimezone(UTC)


def nights_between(check_in: date, check_out: date) -> list[date]:
    return [check_in + timedelta(days=i) for i in range((check_out - check_in).days)]


def check_in_datetime(day: date) -> datetime:
    from common.domain import domain

    hh, mm = (int(x) for x in domain().property.check_in_time.split(":"))
    return datetime.combine(day, time(hh, mm), tzinfo=property_tz())
