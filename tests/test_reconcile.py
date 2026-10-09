from datetime import UTC, datetime, timedelta

import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    DivergenceKind,
    ImbalancePrice,
    ReconciliationReport,
    reconcile_imbalance_prices,
)

START = datetime(2026, 1, 15, tzinfo=UTC)
HOUR = timedelta(hours=1)


def record(
    source: str,
    *,
    zone: BiddingZone = BiddingZone.DK1,
    start: datetime = START,
    price: float | None = 10.0,
    direction: Direction | None = Direction.UP,
    resolution: timedelta = INTERVAL,
) -> ImbalancePrice:
    return ImbalancePrice(
        start=start,
        zone=zone,
        imbalance_price_eur=price,
        imbalance_price_dkk=None,
        spot_price_eur=None,
        dominating_direction=direction,
        satisfied_demand_mw=None,
        afrr_up_vwa_eur=None,
        afrr_down_vwa_eur=None,
        mfrr_up_price_eur=None,
        mfrr_down_price_eur=None,
        source=source,
        resolution=resolution,
        raw={},
    )


def kinds(report: ReconciliationReport) -> list[DivergenceKind]:
    return [d.kind for d in report.divergences]


def test_identical_records_produce_no_divergences() -> None:
    report = reconcile_imbalance_prices([record("a")], [record("b")])

    assert report.primary_source == "a"
    assert report.reference_source == "b"
    assert report.zones == (BiddingZone.DK1,)
    assert report.compared == 1
    assert report.identical == 1
    assert report.divergences == ()
    assert report.counts() == {}


def test_missing_in_reference() -> None:
    later = START + INTERVAL
    report = reconcile_imbalance_prices([record("a"), record("a", start=later)], [record("b")])

    assert report.compared == 2
    assert report.identical == 1
    assert kinds(report) == [DivergenceKind.MISSING_IN_REFERENCE]
    divergence = report.divergences[0]
    assert (divergence.zone, divergence.start) == (BiddingZone.DK1, later)
    assert divergence.primary is not None and divergence.reference is None


def test_missing_in_primary() -> None:
    later = START + INTERVAL
    report = reconcile_imbalance_prices([record("a")], [record("b"), record("b", start=later)])

    assert kinds(report) == [DivergenceKind.MISSING_IN_PRIMARY]
    divergence = report.divergences[0]
    assert divergence.primary is None and divergence.reference is not None


def test_unpublished_on_exactly_one_side() -> None:
    report = reconcile_imbalance_prices(
        [record("a", price=None), record("a", start=START + INTERVAL)],
        [record("b"), record("b", start=START + INTERVAL, price=None)],
    )

    assert kinds(report) == [DivergenceKind.UNPUBLISHED, DivergenceKind.UNPUBLISHED]
    assert report.compared == 2
    assert report.identical == 0


def test_unpublished_on_both_sides_is_identical() -> None:
    report = reconcile_imbalance_prices([record("a", price=None)], [record("b", price=None)])

    assert report.identical == 1
    assert report.divergences == ()


def test_price_divergence_above_tolerance() -> None:
    report = reconcile_imbalance_prices([record("a", price=10.0)], [record("b", price=10.5)])

    assert kinds(report) == [DivergenceKind.PRICE]
    assert "10.0" in report.divergences[0].detail


def test_rounding_divergence_within_tolerance() -> None:
    report = reconcile_imbalance_prices([record("a", price=10.0)], [record("b", price=10.01)])

    assert kinds(report) == [DivergenceKind.ROUNDING]


def test_difference_exactly_at_tolerance_is_rounding() -> None:
    report = reconcile_imbalance_prices(
        [record("a", price=10.0)], [record("b", price=10.02)], tolerance_eur=0.02
    )

    assert kinds(report) == [DivergenceKind.ROUNDING]


def test_float_noise_at_tolerance_boundary_is_rounding() -> None:
    # 17.53 - 17.52 == 0.010000000000001563 > 0.01 in binary, but sources
    # publish cent-precision decimals, so this is a rounding difference.
    report = reconcile_imbalance_prices([record("a", price=17.53)], [record("b", price=17.52)])

    assert kinds(report) == [DivergenceKind.ROUNDING]


def test_direction_divergence() -> None:
    report = reconcile_imbalance_prices(
        [record("a", direction=Direction.UP)], [record("b", direction=Direction.DOWN)]
    )

    assert kinds(report) == [DivergenceKind.DIRECTION]
    assert report.divergences[0].detail == "UP vs DOWN"


def test_direction_missing_on_one_side_is_not_a_divergence() -> None:
    report = reconcile_imbalance_prices(
        [record("a", direction=None)], [record("b", direction=Direction.DOWN)]
    )

    assert report.identical == 1


def test_resolution_divergence() -> None:
    report = reconcile_imbalance_prices(
        [record("a", resolution=HOUR)], [record("b", resolution=INTERVAL)]
    )

    assert kinds(report) == [DivergenceKind.RESOLUTION]
    assert report.divergences[0].detail == "60min vs 15min"


def test_one_interval_can_have_several_divergences() -> None:
    report = reconcile_imbalance_prices(
        [record("a", resolution=HOUR, price=10.0, direction=Direction.UP)],
        [record("b", resolution=INTERVAL, price=10.005, direction=Direction.DOWN)],
    )

    assert kinds(report) == [
        DivergenceKind.DIRECTION,
        DivergenceKind.RESOLUTION,
        DivergenceKind.ROUNDING,
    ]
    assert report.identical == 0
    assert report.counts() == {
        DivergenceKind.RESOLUTION: 1,
        DivergenceKind.ROUNDING: 1,
        DivergenceKind.DIRECTION: 1,
    }


def test_only_zones_present_in_both_inputs_are_compared() -> None:
    later = START + INTERVAL
    primary = [record("a"), record("a", zone=BiddingZone.DK2, price=99.0)]
    reference = [
        record("b", price=20.0),
        record("b", zone=BiddingZone.FI, start=later),
    ]

    report = reconcile_imbalance_prices(primary, reference)

    assert report.zones == (BiddingZone.DK1,)
    assert report.compared == 1
    assert kinds(report) == [DivergenceKind.PRICE]
    assert all(d.zone == BiddingZone.DK1 for d in report.divergences)


def test_divergences_sorted_by_start_zone_kind() -> None:
    later = START + INTERVAL
    primary = [
        record("a", zone=BiddingZone.FI, start=later, resolution=HOUR),
        record("a", zone=BiddingZone.DK1, price=5.0),
        record("a", zone=BiddingZone.DK1, start=later, direction=Direction.DOWN),
    ]
    reference = [
        record("b", zone=BiddingZone.DK1, price=6.0),
        record("b", zone=BiddingZone.DK1, start=later, direction=Direction.UP),
        record("b", zone=BiddingZone.FI, start=later, resolution=INTERVAL, price=10.01),
    ]

    report = reconcile_imbalance_prices(primary, reference)

    assert [(d.start, d.zone, d.kind) for d in report.divergences] == sorted(
        (d.start, d.zone, d.kind) for d in report.divergences
    )
    assert report.divergences[0].kind == DivergenceKind.PRICE
    assert report.divergences[0].start == START
    assert report.counts() == {
        DivergenceKind.PRICE: 1,
        DivergenceKind.DIRECTION: 1,
        DivergenceKind.RESOLUTION: 1,
        DivergenceKind.ROUNDING: 1,
    }


def test_mixed_sources_in_one_input_rejected() -> None:
    with pytest.raises(ValueError, match="mixes sources"):
        reconcile_imbalance_prices([record("a"), record("other")], [record("b")])


def test_duplicate_key_in_one_input_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        reconcile_imbalance_prices([record("a"), record("a", price=11.0)], [record("b")])


def test_same_key_in_both_inputs_is_not_a_duplicate() -> None:
    report = reconcile_imbalance_prices([record("a")], [record("b")])

    assert report.compared == 1


@pytest.mark.parametrize("tolerance", [-0.5, float("nan"), float("inf"), True])
def test_invalid_tolerance_rejected(tolerance: float) -> None:
    with pytest.raises(ValueError, match="tolerance_eur"):
        reconcile_imbalance_prices([record("a")], [record("b")], tolerance_eur=tolerance)


def test_non_record_rejected() -> None:
    with pytest.raises(ValueError, match="ImbalancePrice"):
        reconcile_imbalance_prices([record("a")], [object()])  # type: ignore[list-item]


def test_empty_input() -> None:
    report = reconcile_imbalance_prices([], [record("b")])

    assert report.primary_source == ""
    assert report.zones == ()
    assert report.compared == 0
    assert report.divergences == ()


def test_zones_in_only_one_input_are_reported_not_silently_dropped() -> None:
    report = reconcile_imbalance_prices(
        [record("a", zone=BiddingZone.DK1), record("a", zone=BiddingZone.FI)],
        [record("b", zone=BiddingZone.DK1), record("b", zone=BiddingZone.DK2)],
    )
    assert report.zones == (BiddingZone.DK1,)
    assert report.compared == 1
    assert report.primary_only_zones == (BiddingZone.FI,)
    assert report.reference_only_zones == (BiddingZone.DK2,)


def test_full_overlap_has_no_coverage_gap() -> None:
    report = reconcile_imbalance_prices([record("a")], [record("b")])
    assert report.primary_only_zones == ()
    assert report.reference_only_zones == ()


def test_report_constructs_without_coverage_fields() -> None:
    # Existing positional construction keeps working (the new fields default to ()).
    report = ReconciliationReport("a", "b", (BiddingZone.DK1,), 0, 0, ())
    assert report.primary_only_zones == ()
    assert report.reference_only_zones == ()
