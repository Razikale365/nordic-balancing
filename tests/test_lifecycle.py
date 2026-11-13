from datetime import UTC, datetime
from typing import Any

import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    ImbalancePrice,
    PlaceholderPattern,
    ReserveCapacity,
    ReserveProduct,
    placeholder_pattern,
)

START = datetime(2026, 1, 15, tzinfo=UTC)


def price(
    *,
    source: str = "energidataservice",
    imbalance: float | None = None,
    up: float | None = None,
    down: float | None = None,
) -> ImbalancePrice:
    return ImbalancePrice(
        start=START,
        zone=BiddingZone.DK1,
        imbalance_price_eur=imbalance,
        imbalance_price_dkk=None,
        spot_price_eur=None,
        dominating_direction=None,
        satisfied_demand_mw=None,
        afrr_up_vwa_eur=up,
        afrr_down_vwa_eur=down,
        mfrr_up_price_eur=None,
        mfrr_down_price_eur=None,
        source=source,
        resolution=INTERVAL,
        raw={},
    )


def capacity(source: str = "entsoe", **raw: Any) -> ReserveCapacity:
    return ReserveCapacity(
        start=START,
        zone=BiddingZone.NO1,
        product=ReserveProduct.MFRR,
        direction=Direction.UP,
        price_eur_per_mw=0.0,
        volume_mw=0.0,
        source=source,
        resolution=INTERVAL,
        raw=raw,
    )


@pytest.mark.parametrize(("up", "down"), [(0.0, None), (None, 0.0), (0.0, 0.0), (0.0, 12.5)])
def test_eds_zero_average_without_a_price_matches(up: float | None, down: float | None) -> None:
    assert placeholder_pattern(price(up=up, down=down)) is PlaceholderPattern.EDS_AFRR_AVERAGE


def test_eds_average_matches_only_while_the_price_is_unset() -> None:
    assert placeholder_pattern(price(imbalance=10.0, up=0.0, down=0.0)) is None
    assert placeholder_pattern(price(imbalance=0.0, up=0.0)) is None


def test_eds_without_a_zero_average_does_not_match() -> None:
    assert placeholder_pattern(price()) is None
    assert placeholder_pattern(price(up=5.0, down=7.0)) is None


def test_zero_average_from_another_source_does_not_match() -> None:
    assert placeholder_pattern(price(source="esett", up=0.0, down=0.0)) is None


def test_entsoe_marker_matches() -> None:
    assert (
        placeholder_pattern(capacity(lone_placeholder=True))
        is PlaceholderPattern.ENTSOE_LONE_PLACEHOLDER
    )


def test_entsoe_record_without_marker_does_not_match() -> None:
    assert placeholder_pattern(capacity()) is None


def test_marker_on_another_source_does_not_match() -> None:
    assert placeholder_pattern(capacity("fingrid", lone_placeholder=True)) is None


@pytest.mark.parametrize("value", [False, 1, "True", None])
def test_marker_must_be_exactly_true(value: object) -> None:
    assert placeholder_pattern(capacity(lone_placeholder=value)) is None


@pytest.mark.parametrize("value", [None, "record", 1.0, {"source": "entsoe"}])
def test_non_record_raises_type_error(value: object) -> None:
    with pytest.raises(TypeError, match="ImbalancePrice or ReserveCapacity"):
        placeholder_pattern(value)  # type: ignore[arg-type]


def test_pattern_values() -> None:
    assert PlaceholderPattern.EDS_AFRR_AVERAGE.value == "eds_afrr_average"
    assert PlaceholderPattern.ENTSOE_LONE_PLACEHOLDER.value == "entsoe_lone_placeholder"
