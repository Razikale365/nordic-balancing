"""Cross-provider reconciliation of imbalance price and reserve capacity series.

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

from nordic_balancing.models import (
    BiddingZone,
    Direction,
    ImbalancePrice,
    ReserveCapacity,
    ReserveProduct,
)


class DivergenceKind(StrEnum):
    """How one interval differs between the primary and the reference series."""

    MISSING_IN_PRIMARY = "missing_in_primary"
    MISSING_IN_REFERENCE = "missing_in_reference"
    UNPUBLISHED = "unpublished"
    PRICE = "price"
    ROUNDING = "rounding"
    DIRECTION = "direction"
    RESOLUTION = "resolution"
    VOLUME = "volume"


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
    ``identical`` is how many of them produced no divergence: present in both,
    same ``resolution``, exactly equal prices or both ``None`` (a difference
    within the tolerance is a ROUNDING divergence, and a non-finite price is
    always a PRICE divergence), and no conflicting
    ``dominating_direction`` when both sides have one. Other fields (component
    prices, ``raw``) are not compared. Sources are
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
    ``tolerance_eur`` bounds ROUNDING: a price difference above it is PRICE
    unless it exceeds the tolerance only through binary float noise
    (``math.isclose`` against the tolerance), and a non-zero one at or below it
    is ROUNDING. A non-finite price (NaN or infinity) is never identical or
    ROUNDING; it is always PRICE. PRIMARY/REFERENCE names follow
    the argument order. Divergences are ordered by ``(start, zone, kind)``.
    The result describes differences; it never merges the series and never
    decides which source is right.
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
        # The published side may itself be NaN or inf: that is always PRICE too.
        published = p if p is not None else r
        if published is not None and not math.isfinite(published):
            divergences.append(
                Divergence(
                    zone, start, DivergenceKind.PRICE, primary, reference, f"{p} vs {r} EUR/MWh"
                )
            )
    elif p is not None and r is not None:
        diff = abs(p - r)
        # A non-finite diff means a NaN or inf price on at least one side; it is
        # never equal and never within the tolerance, so it is a PRICE divergence.
        # Sources publish cent-precision decimals; a diff that only exceeds the
        # tolerance through binary float noise (17.53 - 17.52 > 0.01) is ROUNDING.
        if not math.isfinite(diff) or (
            diff > tolerance_eur and not math.isclose(diff, tolerance_eur)
        ):
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


@dataclass(frozen=True, slots=True)
class CapacityDivergence:
    """One classified difference for one zone, product, direction and interval.

    The same key can yield several divergences. ``primary``/``reference`` hold
    the offending records; the side missing the interval is ``None``.
    """

    zone: BiddingZone
    product: ReserveProduct
    direction: Direction
    start: datetime
    kind: DivergenceKind
    primary: ReserveCapacity | None
    reference: ReserveCapacity | None
    detail: str


@dataclass(frozen=True, slots=True)
class CapacityReconciliationReport:
    """The outcome of one reserve capacity reconciliation run.

    ``zones`` and ``products`` are those present in both inputs. ``compared``
    counts the union of (zone, product, direction, start) keys over those zones
    and products; ``identical`` counts keys producing no divergence. Non-zero
    price differences within tolerance are ROUNDING; volume differences within
    tolerance produce no divergence. Non-finite values always diverge.
    Sources are empty strings when an input was empty. Zones and products
    present in only one input are listed in the corresponding coverage fields
    rather than compared, so coverage gaps are never silent. ``raw`` is not
    compared.
    """

    primary_source: str
    reference_source: str
    zones: tuple[BiddingZone, ...]
    products: tuple[ReserveProduct, ...]
    compared: int
    identical: int
    divergences: tuple[CapacityDivergence, ...]
    primary_only_zones: tuple[BiddingZone, ...] = ()
    reference_only_zones: tuple[BiddingZone, ...] = ()
    primary_only_products: tuple[ReserveProduct, ...] = ()
    reference_only_products: tuple[ReserveProduct, ...] = ()

    def counts(self) -> dict[DivergenceKind, int]:
        """Divergence totals per kind."""
        return dict(Counter(d.kind for d in self.divergences))


def reconcile_reserve_capacity(
    primary: Iterable[ReserveCapacity],
    reference: Iterable[ReserveCapacity],
    *,
    tolerance_eur_per_mw: float = 0.01,
    tolerance_mw: float = 0.0,
) -> CapacityReconciliationReport:
    """Classify every difference between two reserve capacity series.

    Each input must contain only ``ReserveCapacity`` records from one ``source``
    and must not repeat a (zone, product, direction, start) key. Only zones AND
    products present in both inputs are compared; the others are listed in the
    report's coverage fields. Directions are separate keys, including
    ``Direction.NONE`` for symmetric FCR-N; they are not reconciled against
    each other.
    ``tolerance_eur_per_mw`` bounds ROUNDING: a non-zero price difference within
    it is ROUNDING, and a difference above it is PRICE unless it exceeds the
    tolerance only through binary float noise (``math.isclose`` against the
    tolerance). Volume differences above ``tolerance_mw`` use the same float-noise
    rule and are VOLUME; those within it produce no divergence. Both tolerances
    must be finite, non-negative numbers, not bool. A non-finite price or volume
    is always PRICE or VOLUME, including against an unpublished value. Exactly
    one unpublished side yields UNPUBLISHED, once per key for price and volume
    combined. Missing records yield only MISSING_IN_PRIMARY/MISSING_IN_REFERENCE.
    PRIMARY/REFERENCE names follow the argument order. Divergences are ordered
    by ``(start, zone, product, direction, kind)``, using enum values.
    The result describes differences; it never merges the series and never
    decides which source is right.
    """
    for name, tolerance in (
        ("tolerance_eur_per_mw", tolerance_eur_per_mw),
        ("tolerance_mw", tolerance_mw),
    ):
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, int | float)
            or not math.isfinite(tolerance)
            or tolerance < 0
        ):
            raise ValueError(f"{name} must be a finite non-negative number")
    primary_source, primary_records = _index_capacity(primary, "primary")
    reference_source, reference_records = _index_capacity(reference, "reference")
    primary_zones = {key[0] for key in primary_records}
    reference_zones = {key[0] for key in reference_records}
    primary_products = {key[1] for key in primary_records}
    reference_products = {key[1] for key in reference_records}
    common_zones = primary_zones & reference_zones
    common_products = primary_products & reference_products
    keys = {
        key
        for key in (*primary_records, *reference_records)
        if key[0] in common_zones and key[1] in common_products
    }
    divergences: list[CapacityDivergence] = []
    identical = 0
    for zone, product, direction, start in keys:
        key = (zone, product, direction, start)
        found = _compare_capacity(
            zone,
            product,
            direction,
            start,
            primary_records.get(key),
            reference_records.get(key),
            tolerance_eur_per_mw,
            tolerance_mw,
        )
        if found:
            divergences.extend(found)
        else:
            identical += 1
    divergences.sort(
        key=lambda d: (d.start, d.zone.value, d.product.value, d.direction.value, d.kind.value)
    )
    return CapacityReconciliationReport(
        primary_source=primary_source,
        reference_source=reference_source,
        zones=tuple(sorted(common_zones)),
        products=tuple(sorted(common_products, key=lambda p: p.value)),
        compared=len(keys),
        identical=identical,
        divergences=tuple(divergences),
        primary_only_zones=tuple(sorted(primary_zones - reference_zones)),
        reference_only_zones=tuple(sorted(reference_zones - primary_zones)),
        primary_only_products=tuple(
            sorted(primary_products - reference_products, key=lambda p: p.value)
        ),
        reference_only_products=tuple(
            sorted(reference_products - primary_products, key=lambda p: p.value)
        ),
    )


def _index_capacity(
    records: Iterable[ReserveCapacity], label: str
) -> tuple[str, dict[tuple[BiddingZone, ReserveProduct, Direction, datetime], ReserveCapacity]]:
    """Key records by zone, product, direction and start, enforcing one source."""
    source: str | None = None
    indexed: dict[tuple[BiddingZone, ReserveProduct, Direction, datetime], ReserveCapacity] = {}
    for record in records:
        if not isinstance(record, ReserveCapacity):
            raise ValueError(f"{label} must contain only ReserveCapacity records")
        if source is None:
            source = record.source
        elif record.source != source:
            raise ValueError(f"{label} mixes sources {source!r} and {record.source!r}")
        key = (record.zone, record.product, record.direction, record.start)
        if key in indexed:
            raise ValueError(
                f"{label}: duplicate record for {record.zone.value} "
                f"{record.product.value} {record.direction.name} at {record.start.isoformat()}"
            )
        indexed[key] = record
    return source or "", indexed


def _compare_capacity(
    zone: BiddingZone,
    product: ReserveProduct,
    direction: Direction,
    start: datetime,
    primary: ReserveCapacity | None,
    reference: ReserveCapacity | None,
    tolerance_eur_per_mw: float,
    tolerance_mw: float,
) -> list[CapacityDivergence]:
    divergences: list[CapacityDivergence] = []

    def add(kind: DivergenceKind, detail: str) -> None:
        divergences.append(
            CapacityDivergence(zone, product, direction, start, kind, primary, reference, detail)
        )

    if primary is None:
        assert reference is not None  # compared keys come from the union
        add(DivergenceKind.MISSING_IN_PRIMARY, f"only in {reference.source}")
        return divergences
    if reference is None:
        add(DivergenceKind.MISSING_IN_REFERENCE, f"only in {primary.source}")
        return divergences
    if primary.resolution != reference.resolution:
        add(
            DivergenceKind.RESOLUTION,
            f"{_minutes(primary.resolution)} vs {_minutes(reference.resolution)}",
        )
    unpublished = []
    for field, p, r, tolerance, kind, unit in (
        (
            "price",
            primary.price_eur_per_mw,
            reference.price_eur_per_mw,
            tolerance_eur_per_mw,
            DivergenceKind.PRICE,
            "EUR/MW",
        ),
        (
            "volume",
            primary.volume_mw,
            reference.volume_mw,
            tolerance_mw,
            DivergenceKind.VOLUME,
            "MW",
        ),
    ):
        if (p is None) != (r is None):
            unpublished.append(f"{field} {_price(p)} vs {_price(r)}")
            published = p if p is not None else r
            assert published is not None
            if not math.isfinite(published):
                add(kind, f"{p} vs {r} {unit}")
        elif p is not None and r is not None:
            diff = abs(p - r)
            if not math.isfinite(diff) or (diff > tolerance and not math.isclose(diff, tolerance)):
                add(kind, f"{p} vs {r} {unit}")
            elif kind is DivergenceKind.PRICE and diff > 0:
                add(DivergenceKind.ROUNDING, f"{p} vs {r} {unit}")
    if unpublished:
        add(DivergenceKind.UNPUBLISHED, "; ".join(unpublished))
    return divergences


def _minutes(resolution: timedelta) -> str:
    return f"{int(resolution.total_seconds() // 60)}min"


def _price(value: float | None) -> str:
    return "unpublished" if value is None else str(value)
