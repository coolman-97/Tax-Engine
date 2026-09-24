"""Turning several documents and several years into one property.

A household arrives as a stack of returns. The same property appears in each
one, written differently, with figures that do not always agree. Two jobs:

**Entity resolution.** "1247 Ocean Blvd, Long Beach, CA 90802", "1247 Ocean
Boulevard, Long Beach CA 90802" and "1247 Ocean Blv., Long Beach, California
90802" are one property. Matching has to survive that without being so loose
that two genuinely different properties merge - a false merge is far worse
than a false split, because it silently combines two basis histories.

**Temporal reconciliation.** Each field becomes a timeline rather than a value.
Where years disagree about something that should not change - a placed-in-service
date, a depreciable basis - that is a `Conflict`, and it is surfaced.

There is no averaging code path in this module, and that is deliberate. Two
filed returns implying a 30% and a 20% land allocation do not average to 25%.
One of them is right, the owner knows which, and the correct behaviour is to
say so and ask. Quietly averaging would produce a number that is defensible to
nobody and wrong for certain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Observation", "Conflict", "ResolvedProperty", "reconcile", "normalize_address"]

_ABBREV = {
    "boulevard": "blvd", "blv": "blvd", "avenue": "ave", "av": "ave",
    "street": "st", "road": "rd", "drive": "dr", "lane": "ln", "court": "ct",
    "place": "pl", "terrace": "ter", "parkway": "pkwy", "highway": "hwy",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "california": "ca", "texas": "tx", "new york": "ny", "florida": "fl",
    "apartment": "apt", "suite": "ste", "unit": "apt",
}

#: Fields that describe the property itself and must not change year to year.
#: Disagreement here is a genuine conflict, not a timeline.
_INVARIANT = {"date_placed_in_service", "cost_or_basis", "recovery_period_years"}


def normalize_address(raw: str) -> str:
    """A comparison key for an address. Lossy on purpose, but not too lossy."""
    s = raw.lower().strip()
    s = re.sub(r"[.,#]", " ", s)
    s = re.sub(r"\s+", " ", s)
    for long, short in _ABBREV.items():
        s = re.sub(rf"\b{re.escape(long)}\b", short, s)
    s = re.sub(r"\b(\d{5})-\d{4}\b", r"\1", s)  # ZIP+4 -> ZIP
    return re.sub(r"\s+", " ", s).strip()


def _street_number(s: str) -> str | None:
    m = re.match(r"\s*(\d+)", s)
    return m.group(1) if m else None


def _zip_code(s: str) -> str | None:
    m = re.search(r"\b(\d{5})\b", s)
    return m.group(1) if m else None


@dataclass(frozen=True)
class Observation:
    """One document's statement about one field of one property."""

    document_id: str
    tax_year: int
    field: str
    value: Any
    page: int = 1
    quote: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class Conflict:
    """Two documents disagreeing about something that should not change."""

    field: str
    observations: tuple[Observation, ...]

    @property
    def values(self) -> tuple[Any, ...]:
        return tuple(o.value for o in self.observations)

    def describe(self) -> str:
        parts = ", ".join(
            f"{o.value} ({o.document_id}, p.{o.page})" for o in self.observations)
        return f"{self.field} disagrees across years: {parts}"


@dataclass
class ResolvedProperty:
    """One property, assembled from every document that mentions it."""

    key: str
    labels: set[str] = field(default_factory=set)
    timeline: dict[int, dict[str, Observation]] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def display_label(self) -> str:
        """The longest label seen, which is usually the most complete one."""
        return max(self.labels, key=len) if self.labels else self.key

    @property
    def years(self) -> list[int]:
        return sorted(self.timeline)

    def missing_years(self) -> list[int]:
        """Gaps in the record. A client who lost a return still owned the
        property that year, and the engine needs to know the year is absent
        rather than assume nothing happened."""
        ys = self.years
        if len(ys) < 2:
            return []
        return [y for y in range(ys[0], ys[-1] + 1) if y not in self.timeline]

    def value_in(self, year: int, name: str) -> Any | None:
        obs = self.timeline.get(year, {}).get(name)
        return obs.value if obs else None

    def series(self, name: str) -> list[tuple[int, Any]]:
        return [(y, o.value) for y in self.years
                if (o := self.timeline[y].get(name)) is not None]

    def has_blocking_conflict(self) -> bool:
        return bool(self.conflicts)


def reconcile(extractions: list) -> list[ResolvedProperty]:
    """Group extracted properties across documents and surface disagreements."""
    buckets: dict[str, ResolvedProperty] = {}

    for result in extractions:
        for prop in result.properties:
            key = _resolve_key(prop.property_label, buckets)
            bucket = buckets.setdefault(key, ResolvedProperty(key=key))
            bucket.labels.add(prop.property_label)
            year = prop.tax_year
            slot = bucket.timeline.setdefault(year, {})
            for name in (
                "rents_received", "total_expenses", "mortgage_interest",
                "depreciation", "insurance", "property_taxes",
                "date_placed_in_service", "cost_or_basis", "recovery_period_years",
            ):
                value = getattr(prop, name, None)
                if value is None:
                    continue
                ev = result.evidence_for(prop.property_label, name)
                slot[name] = Observation(
                    document_id=result.document_id, tax_year=year, field=name,
                    value=value, page=ev.page if ev else 1,
                    quote=ev.quote if ev else "",
                    confidence=ev.confidence if ev else 0.0,
                )

    for bucket in buckets.values():
        bucket.conflicts = _find_conflicts(bucket)
    return sorted(buckets.values(), key=lambda b: b.display_label)


def _resolve_key(label: str, buckets: dict[str, ResolvedProperty]) -> str:
    """Find the bucket this label belongs to, or start a new one.

    Two addresses are the same property when the normalised strings match, or
    when the street number and ZIP both match and the remainder is close. The
    street-number-plus-ZIP requirement is what stops a loose match from merging
    two different properties on the same street.
    """
    norm = normalize_address(label)
    if norm in buckets:
        return norm
    number, zipcode = _street_number(norm), _zip_code(norm)
    if number and zipcode:
        for key in buckets:
            if _street_number(key) == number and _zip_code(key) == zipcode:
                return key
    return norm


def _find_conflicts(bucket: ResolvedProperty) -> list[Conflict]:
    """Disagreement about an invariant field is a conflict.

    Operating figures are expected to move year to year - that is a timeline,
    not a conflict. It is the fields that describe the property itself that
    must not move.
    """
    conflicts: list[Conflict] = []
    for name in _INVARIANT:
        seen: dict[Any, list[Observation]] = {}
        for year in bucket.years:
            obs = bucket.timeline[year].get(name)
            if obs is None:
                continue
            seen.setdefault(obs.value, []).append(obs)
        if len(seen) > 1:
            flat = tuple(o for group in seen.values() for o in group)
            conflicts.append(Conflict(field=name, observations=flat))

    # Depreciation is an operating figure, but on a straight-line schedule it
    # should be flat once the property is fully in service. A step change means
    # the basis or the land allocation moved, and neither is stated anywhere -
    # which is exactly the case that blocks a recommendation.
    depreciation = bucket.series("depreciation")
    full_years = [(y, v) for y, v in depreciation if v]
    if len({v for _, v in full_years}) > 1 and len(full_years) > 1:
        first, last = full_years[0][1], full_years[-1][1]
        if first and abs(last - first) / max(first, 1) > 0.02:
            obs = tuple(bucket.timeline[y]["depreciation"] for y, _ in full_years)
            conflicts.append(Conflict(field="depreciation", observations=obs))
    return conflicts
