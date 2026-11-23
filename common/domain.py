"""Typed view over the domain pack (data/domain/*.yaml).

The domain pack is the only thing that changes when Concierge is re-targeted
from a hotel to car rental, tours or tickets.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from common.config import get_settings


class PropertyInfo(BaseModel):
    id: str
    name: str
    tagline: str = ""
    currency: str = "USD"
    timezone: str = "UTC"
    check_in_time: str = "15:00"
    check_out_time: str = "11:00"
    tax_rate: float = 0.0
    reservation_prefix: str = "RES"
    contact_email: str = ""
    contact_phone: str = ""


class RoomType(BaseModel):
    code: str
    name: str
    standard_occupancy: int
    max_guests: int
    inventory: int
    base_rate: float
    bed: str = ""
    size_sqm: int = 0
    view: str = ""
    pets_allowed: bool = False
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class SeasonRule(BaseModel):
    name: str
    start: str  # MM-DD
    end: str  # MM-DD (inclusive, may wrap the year)
    pct: float

    def contains(self, day: date) -> bool:
        md = day.strftime("%m-%d")
        if self.start <= self.end:
            return self.start <= md <= self.end
        return md >= self.start or md <= self.end


class OccupancyRule(BaseModel):
    min: float
    pct: float


class LengthOfStayRule(BaseModel):
    min_nights: int
    pct: float


class PromoCode(BaseModel):
    pct: float
    valid_from: date | None = None
    valid_to: date | None = None
    min_nights: int | None = None
    description: str = ""


class WeekendRule(BaseModel):
    nights: list[str]
    pct: float


class RateRules(BaseModel):
    weekend: WeekendRule
    seasons: list[SeasonRule] = Field(default_factory=list)
    occupancy: list[OccupancyRule] = Field(default_factory=list)
    length_of_stay: list[LengthOfStayRule] = Field(default_factory=list)
    promo_codes: dict[str, PromoCode] = Field(default_factory=dict)
    extra_guest_fee_per_night: float = 0.0
    quote_ttl_minutes: int = 30


class Policies(BaseModel):
    free_cancellation_hours: int = 48
    peak_free_cancellation_days: int = 7
    late_cancellation_charge_nights: int = 1
    modification_cutoff_hours: int = 48
    max_rooms_per_booking: int = 8
    max_nights: int = 28
    pet_fee_per_night: float = 0.0
    children_free_under: int = 12


class DomainPack(BaseModel):
    property: PropertyInfo
    room_types: list[RoomType]
    rate_rules: RateRules
    policies: Policies

    def room(self, code: str) -> RoomType | None:
        code = (code or "").upper()
        return next((r for r in self.room_types if r.code == code), None)

    def peak_season(self) -> SeasonRule | None:
        peaks = [s for s in self.rate_rules.seasons if s.pct > 0]
        return max(peaks, key=lambda s: s.pct) if peaks else None

    def resolve_room_alias(self, text: str) -> str | None:
        """Map free text ('sea view', 'family suite') to a room code."""
        import re

        lowered = text.lower()
        best: tuple[int, str] | None = None
        for room in self.room_types:
            for alias in [room.code.lower(), room.name.lower(), *[a.lower() for a in room.aliases]]:
                if not alias or (best is not None and len(alias) <= best[0]):
                    continue
                if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
                    best = (len(alias), room.code)
        return best[1] if best else None


def load_domain_pack(path: Path | None = None) -> DomainPack:
    path = path or get_settings().domain_pack
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return DomainPack.model_validate(raw)


@lru_cache(maxsize=4)
def _cached(path: str) -> DomainPack:
    return load_domain_pack(Path(path))


def domain() -> DomainPack:
    return _cached(str(get_settings().domain_pack))
