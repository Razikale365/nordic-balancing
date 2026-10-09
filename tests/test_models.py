from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    ImbalancePrice,
    ReserveCapacity,
    ReserveProduct,
)

START = datetime(2026, 1, 15, tzinfo=UTC)


def price(raw: dict[str, Any]) -> ImbalancePrice:
    return ImbalancePrice(
        start=START,
        zone=BiddingZone.FI,
        imbalance_price_eur=1.0,
        imbalance_price_dkk=None,
        spot_price_eur=None,
        dominating_direction=None,
        satisfied_demand_mw=None,
        afrr_up_vwa_eur=None,
        afrr_down_vwa_eur=None,
        mfrr_up_price_eur=None,
        mfrr_down_price_eur=None,
        source="test",
        resolution=INTERVAL,
        raw=raw,
    )


def test_raw_is_detached_from_the_source_record() -> None:
    nested: dict[str, Any] = {"value": 1}
    items: list[Any] = [1, {"x": 2}]
    record = price({"319": nested, "list": items})
    nested["value"] = 99
    items.append(3)
    items[1]["x"] = 99
    assert record.raw["319"] == {"value": 1}
    assert record.raw["list"] == [1, {"x": 2}]


def test_expanded_records_do_not_share_nested_raw() -> None:
    first = price({"319": {"value": 1}})
    second = replace(first, start=START + INTERVAL)
    second.raw["319"]["value"] = 99
    assert first.raw["319"] == {"value": 1}


def test_raw_top_level_is_read_only() -> None:
    record = price({"a": 1})
    with pytest.raises(TypeError):
        record.raw["a"] = 2  # type: ignore[index]


def test_raw_preserves_json_like_types() -> None:
    raw = {"t": (1, 2), "l": [1], "d": {"k": None}, "n": 1.5, "s": "x"}
    assert dict(price(raw).raw) == raw


def test_reserve_capacity_raw_is_detached_too() -> None:
    nested = {"v": 1}
    capacity = ReserveCapacity(
        start=START,
        zone=BiddingZone.SE3,
        product=ReserveProduct.MFRR,
        direction=Direction.UP,
        price_eur_per_mw=1.0,
        volume_mw=2.0,
        source="test",
        resolution=INTERVAL,
        raw={"nested": nested},
    )
    nested["v"] = 99
    assert capacity.raw["nested"] == {"v": 1}
