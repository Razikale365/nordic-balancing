import pickle
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    CapacityDivergence,
    CapacityReconciliationReport,
    Direction,
    DivergenceKind,
    ReserveCapacity,
    ReserveProduct,
    reconcile_reserve_capacity,
)

START = datetime(2026, 1, 15, tzinfo=UTC)
HOUR = timedelta(hours=1)


def record(
    source: str,
    *,
    zone: BiddingZone = BiddingZone.FI,
    product: ReserveProduct = ReserveProduct.MFRR,
    direction: Direction = Direction.UP,
    start: datetime = START,
    price: float | None = 10.0,
    volume: float | None = 261.0,
    resolution: timedelta = INTERVAL,
) -> ReserveCapacity:
    return ReserveCapacity(
        start=start,
        zone=zone,
        product=product,
        direction=direction,
        price_eur_per_mw=price,
        volume_mw=volume,
        source=source,
        resolution=resolution,
        raw={},
    )


def numeric_record(source: str, field: str, value: float | None) -> ReserveCapacity:
    return record(source, price=value) if field == "price" else record(source, volume=value)


def kinds(report: CapacityReconciliationReport) -> list[DivergenceKind]:
    return [d.kind for d in report.divergences]


def test_identical_records_produce_no_divergences() -> None:
    primary, reference = record("a"), record("b")
    report = reconcile_reserve_capacity(iter([primary]), iter([reference]))
    assert report.primary_source == "a"
    assert report.reference_source == "b"
    assert report.zones == (BiddingZone.FI,)
    assert report.products == (ReserveProduct.MFRR,)
    assert report.compared == report.identical == 1
    assert report.divergences == ()
    assert report.counts() == {}
    assert report.primary_only_zones == report.reference_only_zones == ()
    assert report.primary_only_products == report.reference_only_products == ()
    assert primary == record("a") and reference == record("b")


@pytest.mark.parametrize("missing_primary", [True, False])
def test_missing_on_each_side_yields_only_missing(missing_primary: bool) -> None:
    extra = record("a", start=START + INTERVAL, price=float("nan"), resolution=HOUR)
    primary, reference = [record("a"), extra], [record("b")]
    if missing_primary:
        primary, reference = reference, primary
    report = reconcile_reserve_capacity(primary, reference)
    kind = (
        DivergenceKind.MISSING_IN_PRIMARY
        if missing_primary
        else DivergenceKind.MISSING_IN_REFERENCE
    )
    assert report.compared == 2 and report.identical == 1
    assert kinds(report) == [kind]
    divergence = report.divergences[0]
    assert (divergence.zone, divergence.product, divergence.direction, divergence.start) == (
        BiddingZone.FI,
        ReserveProduct.MFRR,
        Direction.UP,
        START + INTERVAL,
    )
    assert divergence.primary == (None if missing_primary else extra)
    assert divergence.reference == (extra if missing_primary else None)
    assert divergence.detail == "only in a"


def test_resolution_divergence() -> None:
    report = reconcile_reserve_capacity([record("a", resolution=HOUR)], [record("b")])
    assert kinds(report) == [DivergenceKind.RESOLUTION]
    assert report.divergences[0].detail == "60min vs 15min"
    assert report.identical == 0


@pytest.mark.parametrize(
    ("primary_price", "reference_price", "tolerance", "expected"),
    [
        (0.0, 0.01, 0.01, DivergenceKind.ROUNDING),
        (0.0, 0.009, 0.01, DivergenceKind.ROUNDING),
        (0.0, 0.011, 0.01, DivergenceKind.PRICE),
        (17.53, 17.52, 0.01, DivergenceKind.ROUNDING),
        (10.0, 10.02, 0.02, DivergenceKind.ROUNDING),
        (10.0, 10.02, 0.01, DivergenceKind.PRICE),
        (0.0, 0.001, 0, DivergenceKind.PRICE),
        (-2.0, -1.0, 0.01, DivergenceKind.PRICE),
    ],
)
def test_price_and_rounding_tolerance_boundaries(
    primary_price: float, reference_price: float, tolerance: float, expected: DivergenceKind
) -> None:
    report = reconcile_reserve_capacity(
        [record("a", price=primary_price)],
        [record("b", price=reference_price)],
        tolerance_eur_per_mw=tolerance,
    )
    assert kinds(report) == [expected]
    assert report.divergences[0].detail == f"{primary_price} vs {reference_price} EUR/MW"
    assert report.identical == 0


def test_default_tolerances_are_one_cent_and_zero_mw() -> None:
    def kinds_for(primary: ReserveCapacity, reference: ReserveCapacity) -> list[DivergenceKind]:
        return kinds(reconcile_reserve_capacity([primary], [reference]))

    assert kinds_for(record("a", price=10.0), record("b", price=10.01)) == [DivergenceKind.ROUNDING]
    assert kinds_for(record("a", price=10.0), record("b", price=10.02)) == [DivergenceKind.PRICE]
    assert kinds_for(record("a", volume=1.0), record("b", volume=1.001)) == [DivergenceKind.VOLUME]


@pytest.mark.parametrize(
    ("primary_volume", "reference_volume", "tolerance", "diverges"),
    [
        (261.0, 0.0, 0.0, True),
        (1.0, 1.005, 0.0, True),
        (1.0, 1.005, 0.01, False),
        (0.0, 0.01, 0.01, False),
        (0.0, 0.011, 0.01, True),
        (17.53, 17.52, 0.01, False),
        (1.0, 1.0, 0, False),
        (-1.0, -2.0, 1, False),
    ],
)
def test_volume_default_and_custom_tolerance(
    primary_volume: float, reference_volume: float, tolerance: float, diverges: bool
) -> None:
    report = reconcile_reserve_capacity(
        [record("a", volume=primary_volume)],
        [record("b", volume=reference_volume)],
        tolerance_mw=tolerance,
    )
    assert kinds(report) == ([DivergenceKind.VOLUME] if diverges else [])
    assert report.identical == (0 if diverges else 1)
    if diverges:
        assert report.divergences[0].detail == f"{primary_volume} vs {reference_volume} MW"


@pytest.mark.parametrize("field", ["price", "volume"])
@pytest.mark.parametrize(
    ("p", "r"),
    [
        (float("nan"), 5.0),
        (5.0, float("nan")),
        (float("nan"), float("nan")),
        (float("inf"), float("inf")),
        (-float("inf"), -float("inf")),
        (float("inf"), 5.0),
        (5.0, -float("inf")),
        (1e308, -1e308),
    ],
)
def test_non_finite_prices_and_volumes_diverge(field: str, p: float, r: float) -> None:
    report = reconcile_reserve_capacity(
        [numeric_record("a", field, p)], [numeric_record("b", field, r)]
    )
    assert kinds(report) == [DivergenceKind.PRICE if field == "price" else DivergenceKind.VOLUME]
    assert report.identical == 0


@pytest.mark.parametrize("field", ["price", "volume"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("published_primary", [True, False])
def test_non_finite_against_unpublished_also_diverges(
    field: str, value: float, published_primary: bool
) -> None:
    p, r = (value, None) if published_primary else (None, value)
    report = reconcile_reserve_capacity(
        [numeric_record("a", field, p)], [numeric_record("b", field, r)]
    )
    expected = DivergenceKind.PRICE if field == "price" else DivergenceKind.VOLUME
    assert report.counts() == {expected: 1, DivergenceKind.UNPUBLISHED: 1}
    assert report.identical == 0


@pytest.mark.parametrize("missing_primary", [True, False])
@pytest.mark.parametrize("fields", [("price",), ("volume",), ("price", "volume")])
def test_unpublished_once_for_price_volume_or_both(
    missing_primary: bool, fields: tuple[str, ...]
) -> None:
    unpublished = record(
        "a",
        price=None if "price" in fields else 10.0,
        volume=None if "volume" in fields else 261.0,
    )
    published = record("b")
    p, r = (unpublished, published) if missing_primary else (published, unpublished)
    report = reconcile_reserve_capacity([p], [r])
    assert kinds(report) == [DivergenceKind.UNPUBLISHED]
    details = []
    for field in fields:
        value = "10.0" if field == "price" else "261.0"
        sides = f"unpublished vs {value}" if missing_primary else f"{value} vs unpublished"
        details.append(f"{field} {sides}")
    assert report.divergences[0].detail == "; ".join(details)
    assert report.identical == 0


def test_both_unpublished_is_identical() -> None:
    report = reconcile_reserve_capacity(
        [record("a", price=None, volume=None)], [record("b", price=None, volume=None)]
    )
    assert report.identical == 1 and report.divergences == ()


def test_unpublished_does_not_hide_other_field_or_resolution() -> None:
    report = reconcile_reserve_capacity(
        [record("a", price=None, volume=0.0, resolution=HOUR)], [record("b")]
    )
    assert kinds(report) == [
        DivergenceKind.RESOLUTION,
        DivergenceKind.UNPUBLISHED,
        DivergenceKind.VOLUME,
    ]
    report = reconcile_reserve_capacity([record("a", price=0.0, volume=None)], [record("b")])
    assert kinds(report) == [DivergenceKind.PRICE, DivergenceKind.UNPUBLISHED]


def test_zones_and_products_in_only_one_input_are_reported() -> None:
    report = reconcile_reserve_capacity(
        [
            record("a"),
            record("a", zone=BiddingZone.DK2, product=ReserveProduct.FCR_N),
            record("a", zone=BiddingZone.DK1, product=ReserveProduct.FCR_D),
        ],
        [
            record("b"),
            record("b", zone=BiddingZone.NO1, product=ReserveProduct.AFRR),
            record("b", zone=BiddingZone.SE1, product=ReserveProduct.FCR),
        ],
    )
    assert report.zones == (BiddingZone.FI,)
    assert report.products == (ReserveProduct.MFRR,)
    assert report.compared == report.identical == 1
    assert report.primary_only_zones == (BiddingZone.DK1, BiddingZone.DK2)
    assert report.reference_only_zones == (BiddingZone.NO1, BiddingZone.SE1)
    assert report.primary_only_products == (ReserveProduct.FCR_D, ReserveProduct.FCR_N)
    assert report.reference_only_products == (ReserveProduct.FCR, ReserveProduct.AFRR)


def test_common_zones_and_products_are_independent_intersections() -> None:
    report = reconcile_reserve_capacity(
        [record("a", zone=BiddingZone.DK1), record("a", product=ReserveProduct.AFRR)],
        [record("b"), record("b", zone=BiddingZone.DK1, product=ReserveProduct.AFRR)],
    )
    assert report.compared == 4 and report.identical == 0
    assert report.counts() == {
        DivergenceKind.MISSING_IN_PRIMARY: 2,
        DivergenceKind.MISSING_IN_REFERENCE: 2,
    }
    assert report.zones == (BiddingZone.DK1, BiddingZone.FI)
    assert report.products == (ReserveProduct.AFRR, ReserveProduct.MFRR)


@pytest.mark.parametrize("empty_primary", [True, False])
def test_empty_input_reports_coverage(empty_primary: bool) -> None:
    primary, reference = ([], [record("b")]) if empty_primary else ([record("a")], [])
    report = reconcile_reserve_capacity(primary, reference)
    assert report.primary_source == ("" if empty_primary else "a")
    assert report.reference_source == ("b" if empty_primary else "")
    assert report.zones == ()
    assert report.products == ()
    assert report.divergences == ()
    assert report.compared == report.identical == 0
    assert report.primary_only_zones == (() if empty_primary else (BiddingZone.FI,))
    assert report.reference_only_products == ((ReserveProduct.MFRR,) if empty_primary else ())
    assert reconcile_reserve_capacity([], []).counts() == {}


def test_keys_differing_only_in_direction_are_separate() -> None:
    report = reconcile_reserve_capacity(
        [record("a", direction=Direction.DOWN)], [record("b", direction=Direction.UP)]
    )
    assert report.compared == 2 and report.identical == 0
    assert [(d.direction, d.kind) for d in report.divergences] == [
        (Direction.DOWN, DivergenceKind.MISSING_IN_REFERENCE),
        (Direction.UP, DivergenceKind.MISSING_IN_PRIMARY),
    ]
    symmetric = record("a", product=ReserveProduct.FCR_N, direction=Direction.NONE)
    report = reconcile_reserve_capacity([symmetric], [replace(symmetric, source="b")])
    assert report.identical == 1


@pytest.mark.parametrize("label", ["primary", "reference"])
def test_mixed_sources_duplicates_and_wrong_types_rejected(label: str) -> None:
    other = [record("b")]
    for invalid, message in (
        ([record("a"), record("c", start=START + INTERVAL)], f"{label} mixes sources 'a' and 'c'"),
        (
            [record("a"), record("a", price=11.0)],
            f"{label}: duplicate record for FI mFRR UP at {START.isoformat()}",
        ),
        ([object()], f"{label} must contain only ReserveCapacity records"),
    ):
        p, r = (invalid, other) if label == "primary" else (other, invalid)
        with pytest.raises(ValueError) as error:
            reconcile_reserve_capacity(p, r)  # type: ignore[arg-type]
        assert str(error.value) == message


@pytest.mark.parametrize("name", ["tolerance_eur_per_mw", "tolerance_mw"])
@pytest.mark.parametrize(
    "value", [-0.5, float("nan"), float("inf"), -float("inf"), True, False, None, "0.01", 1j]
)
def test_invalid_tolerances_rejected(name: str, value: Any) -> None:
    with pytest.raises(ValueError, match=f"^{name} must be a finite non-negative number$"):
        reconcile_reserve_capacity([], [], **{name: value})


def test_zero_tolerances_and_raw_ignored() -> None:
    p, r = record("a"), replace(record("b"), raw={"extra": [1, 2]})
    report = reconcile_reserve_capacity([p], [r], tolerance_eur_per_mw=0, tolerance_mw=0)
    assert report.identical == 1
    assert dict(r.raw) == {"extra": [1, 2]}


def test_divergences_sorted_by_start_zone_product_direction_kind() -> None:
    keys = [
        (START + INTERVAL, BiddingZone.DK1, ReserveProduct.FCR_N, Direction.NONE),
        (START, BiddingZone.FI, ReserveProduct.MFRR, Direction.UP),
        (START, BiddingZone.DK1, ReserveProduct.MFRR, Direction.UP),
        (START, BiddingZone.DK1, ReserveProduct.FCR_N, Direction.UP),
        (START, BiddingZone.DK1, ReserveProduct.FCR_N, Direction.NONE),
        (START, BiddingZone.DK1, ReserveProduct.FCR_N, Direction.DOWN),
        # Product sorts before direction: this key follows every FCR-N one.
        (START, BiddingZone.DK1, ReserveProduct.MFRR, Direction.DOWN),
    ]
    primary = [
        record("a", start=s, zone=z, product=p, direction=d, price=0.0, volume=0.0, resolution=HOUR)
        for s, z, p, d in keys
    ]
    reference = [
        replace(r, source="b", price_eur_per_mw=1.0, volume_mw=1.0, resolution=INTERVAL)
        for r in reversed(primary)
    ]
    report = reconcile_reserve_capacity(primary, reference)
    expected = [
        (s, z, p, d, kind)
        for s, z, p, d in sorted(keys)
        for kind in (DivergenceKind.PRICE, DivergenceKind.RESOLUTION, DivergenceKind.VOLUME)
    ]
    assert [
        (d.start, d.zone, d.product, d.direction, d.kind) for d in report.divergences
    ] == expected
    assert reconcile_reserve_capacity(reversed(primary), reversed(reference)) == report
    assert report.compared == len(keys) and report.identical == 0
    assert report.counts() == {
        DivergenceKind.PRICE: 7,
        DivergenceKind.RESOLUTION: 7,
        DivergenceKind.VOLUME: 7,
    }
    assert report.products == (ReserveProduct.FCR_N, ReserveProduct.MFRR)


def test_report_and_divergences_are_frozen_slotted_and_pickle() -> None:
    report = reconcile_reserve_capacity([record("a", price=0.0)], [record("b")])
    restored = pickle.loads(pickle.dumps(report))
    assert restored == report
    divergence = restored.divergences[0]
    assert isinstance(divergence, CapacityDivergence)
    assert divergence.primary is not None and dict(divergence.primary.raw) == {}
    for obj in (report, divergence):
        assert not hasattr(obj, "__dict__")
    with pytest.raises(FrozenInstanceError):
        report.compared = 3  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        divergence.detail = "changed"  # type: ignore[misc]
    minimal = CapacityReconciliationReport("a", "b", (), (), 0, 0, ())
    assert minimal.primary_only_zones == minimal.reference_only_zones == ()
    assert minimal.primary_only_products == minimal.reference_only_products == ()


def test_17_oct_2026_fi_mfrr_zero_filled_capacity() -> None:
    stamp = datetime(2026, 10, 17, tzinfo=UTC)
    primary = [
        record("fingrid", start=stamp, price=0.0, volume=0.0),
        record("fingrid", start=stamp, direction=Direction.DOWN, price=0.0, volume=650.0),
    ]
    reference = [
        record("entsoe", start=stamp, price=1.0, volume=261.0),
        record("entsoe", start=stamp, direction=Direction.DOWN, price=2.0, volume=700.0),
    ]
    report = reconcile_reserve_capacity(primary, reference)
    assert report.compared == 2 and report.identical == 0
    assert report.counts() == {DivergenceKind.PRICE: 2, DivergenceKind.VOLUME: 2}
    assert [(d.direction, d.kind, d.detail) for d in report.divergences] == [
        (Direction.DOWN, DivergenceKind.PRICE, "0.0 vs 2.0 EUR/MW"),
        (Direction.DOWN, DivergenceKind.VOLUME, "650.0 vs 700.0 MW"),
        (Direction.UP, DivergenceKind.PRICE, "0.0 vs 1.0 EUR/MW"),
        (Direction.UP, DivergenceKind.VOLUME, "0.0 vs 261.0 MW"),
    ]
