"""Cross-provider reconciliation of imbalance price series.

Reconciliation compares two series covering the same zones and intervals and
classifies every difference it finds. It reports divergences; it never merges
the two series and never decides which source is right.
"""

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from nordic_balancing.models import BiddingZone, ImbalancePrice


class DivergenceKind(StrEnum):
    """How one interval differs between the primary and the reference series."""

    MISSING_IN_PRIMARY = "missing_in_primary"
    MISSING_IN_REFERENCE = "missing_in_reference"
    UNPUBLISHED = "unpublished"
    PRICE = "price"
    ROUNDING = "rounding"
    DIRECTION = "direction"
    RESOLUTION = "resolution"


@dataclass(frozen=True, slots=True)
class Divergence:
    """One classified difference for one zone and one interval start.

    The same interval can yield several divergences — for example RESOLUTION
    together with ROUNDING. ``primary``/``reference`` hold the offending
    records; the side missing the interval is ``None``.
    """

    zone: BiddingZone
    start: datetime
    kind: DivergenceKind
    primary: ImbalancePrice | None
    reference: ImbalancePrice | None
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """The outcome of one reconciliation run.

    ``zones`` are the zones present in both inputs; ``compared`` is the number
    of (zone, start) keys in the union of both inputs over those zones, and
    ``identical`` is how many of them produced no divergence. Sources are
    empty strings when an input was empty. Zones present in only one input are
    not compared; they are listed in ``primary_only_zones`` and
    ``reference_only_zones`` so a coverage gap is never silent.
    """

    primary_source: str
    reference_source: str
    zones: tuple[BiddingZone, ...]
    compared: int
    identical: int
    divergences: tuple[Divergence, ...]
    primary_only_zones: tuple[BiddingZone, ...] = ()
    reference_only_zones: tuple[BiddingZone, ...] = ()

    def counts(self) -> dict[DivergenceKind, int]:
        """Divergence totals per kind."""
        return dict(Counter(d.kind for d in self.divergences))


def reconcile_imbalance_prices(
    primary: Iterable[ImbalancePrice],
    reference: Iterable[ImbalancePrice],
    *,
    tolerance_eur: float = 0.01,
) -> ReconciliationReport:
    """Classify every difference between two imbalance price series.

    Each input must come from a single ``source`` and must not repeat a
    (zone, start) key. Only zones present in both inputs are compared; the others are reported
    as ``primary_only_zones`` / ``reference_only_zones``.
    ``tolerance_eur`` bounds ROUNDING: a price difference above it is PRICE,
    a non-zero one at or below it is ROUNDING. PRIMARY/REFERENCE names follow
    the argument order. The result describes differences; it never merges the
    series and never decides which source is right.
    """
    if (
        isinstance(tolerance_eur, bool)
        or not isinstance(tolerance_eur, int | float)
        or not math.isfinite(tolerance_eur)
        or tolerance_eur < 0
    ):
        raise ValueError("tolerance_eur must be a finite non-negative number")
    primary_source, primary_records = _index(primary, "primary")
    reference_source, reference_records = _index(reference, "reference")
    primary_zones = {zone for zone, _ in primary_records}
    reference_zones = {zone for zone, _ in reference_records}
    common = primary_zones & reference_zones
    keys = {key for key in (*primary_records, *reference_records) if key[0] in common}
    divergences: list[Divergence] = []
    identical = 0
    for zone, start in keys:
        found = _compare(
            zone,
            start,
            primary_records.get((zone, start)),
            reference_records.get((zone, start)),
            tolerance_eur,
        )
        if found:
            divergences.extend(found)
        else:
            identical += 1
    divergences.sort(key=lambda d: (d.start, d.zone, d.kind))
    return ReconciliationReport(
        primary_source=primary_source,
        reference_source=reference_source,
        zones=tuple(sorted(common)),
        compared=len(keys),
        identical=identical,
        divergences=tuple(divergences),
        primary_only_zones=tuple(sorted(primary_zones - reference_zones)),
        reference_only_zones=tuple(sorted(reference_zones - primary_zones)),
    )


def _index(
    records: Iterable[ImbalancePrice], label: str
) -> tuple[str, dict[tuple[BiddingZone, datetime], ImbalancePrice]]:
    """Key records by (zone, start), enforcing one source and no duplicates."""
    source: str | None = None
    indexed: dict[tuple[BiddingZone, datetime], ImbalancePrice] = {}
    for record in records:
        if not isinstance(record, ImbalancePrice):
            raise ValueError(f"{label} must contain only ImbalancePrice records")
        if source is None:
            source = record.source
        elif record.source != source:
            raise ValueError(f"{label} mixes sources {source!r} and {record.source!r}")
        key = (record.zone, record.start)
        if key in indexed:
            raise ValueError(
                f"{label}: duplicate record for {record.zone.value} at {record.start.isoformat()}"
            )
        indexed[key] = record
    return source or "", indexed


def _compare(
    zone: BiddingZone,
    start: datetime,
    primary: ImbalancePrice | None,
    reference: ImbalancePrice | None,
    tolerance_eur: float,
) -> list[Divergence]:
    if reference is not None and primary is None:
        return [
            Divergence(
                zone,
                start,
                DivergenceKind.MISSING_IN_PRIMARY,
                None,
                reference,
                f"only in {reference.source}",
            )
        ]
    if primary is not None and reference is None:
        return [
            Divergence(
                zone,
                start,
                DivergenceKind.MISSING_IN_REFERENCE,
                primary,
                None,
                f"only in {primary.source}",
            )
        ]
    if primary is None or reference is None:
        return []  # unreachable: compared keys come from the union
    divergences = []
    if primary.resolution != reference.resolution:
        divergences.append(
            Divergence(
                zone,
                start,
                DivergenceKind.RESOLUTION,
                primary,
                reference,
                f"{_minutes(primary.resolution)} vs {_minutes(reference.resolution)}",
            )
        )
    p, r = primary.imbalance_price_eur, reference.imbalance_price_eur
    if (p is None) != (r is None):
        divergences.append(
            Divergence(
                zone,
                start,
                DivergenceKind.UNPUBLISHED,
                primary,
                reference,
                f"{_price(p)} vs {_price(r)}",
            )
        )
    elif p is not None and r is not None:
        diff = abs(p - r)
        # Sources publish cent-precision decimals; a diff that only exceeds the
        # tolerance through binary float noise (17.53 - 17.52 > 0.01) is ROUNDING.
        if diff > tolerance_eur and not math.isclose(diff, tolerance_eur):
            divergences.append(
                Divergence(
                    zone, start, DivergenceKind.PRICE, primary, reference, f"{p} vs {r} EUR/MWh"
                )
            )
        elif diff > 0:
            divergences.append(
                Divergence(
                    zone, start, DivergenceKind.ROUNDING, primary, reference, f"{p} vs {r} EUR/MWh"
                )
            )
    if (
        primary.dominating_direction is not None
        and reference.dominating_direction is not None
        and primary.dominating_direction != reference.dominating_direction
    ):
        divergences.append(
            Divergence(
                zone,
                start,
                DivergenceKind.DIRECTION,
                primary,
                reference,
                f"{primary.dominating_direction.name} vs {reference.dominating_direction.name}",
            )
        )
    return divergences


def _minutes(resolution: timedelta) -> str:
    return f"{int(resolution.total_seconds() // 60)}min"


def _price(value: float | None) -> str:
    return "unpublished" if value is None else str(value)
