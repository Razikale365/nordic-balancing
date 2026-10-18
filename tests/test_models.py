from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    ImbalancePrice,
    ReserveCapacity,
    ReserveProduct,
)
from nordic_balancing.models import _copy_raw, to_utc

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


def test_raw_deep_copies_other_mutable_containers() -> None:
    base: dict[str, Any] = {"tags": {1, 2}, "buf": bytearray(b"ab")}
    first, second = price(base), price(base)
    base["tags"].add(3)
    base["buf"][0] = ord("z")
    assert first.raw["tags"] == {1, 2}
    assert bytes(first.raw["buf"]) == b"ab"
    assert first.raw["tags"] is not base["tags"]
    assert first.raw["tags"] is not second.raw["tags"]
    assert first.raw["buf"] is not second.raw["buf"]


_json_like = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False)
    | st.text()
    | st.binary(),
    lambda children: (
        st.lists(children) | st.tuples(children, children) | st.dictionaries(st.text(), children)
    ),
    max_leaves=30,
)


@given(_json_like)
def test_copy_raw_matches_deepcopy_on_json_like_values(value: Any) -> None:
    copied = _copy_raw(value)
    assert copied == deepcopy(value)
    assert type(copied) is type(deepcopy(value))


def test_copy_raw_detaches_at_every_level() -> None:
    inner = {"x": [1]}
    record = price({"a": {"b": inner}, "c": (inner,)})
    inner["x"].append(99)
    assert record.raw["a"]["b"] == {"x": [1]}
    assert record.raw["c"][0] == {"x": [1]}


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


def reserve(start: datetime) -> ReserveCapacity:
    return ReserveCapacity(
        start=start,
        zone=BiddingZone.SE3,
        product=ReserveProduct.MFRR,
        direction=Direction.UP,
        price_eur_per_mw=1.0,
        volume_mw=2.0,
        source="test",
        resolution=INTERVAL,
        raw={},
    )


@pytest.mark.parametrize("kind", ["imbalance", "reserve"])
def test_offset_zero_local_start_is_normalised_to_utc(kind: str) -> None:
    # Europe/London at 01:15 on 2026-10-25 is ambiguous; fold=1 is GMT (offset 0).
    start = datetime(2026, 10, 25, 1, 15, fold=1, tzinfo=ZoneInfo("Europe/London"))
    record = replace(price({}), start=start) if kind == "imbalance" else reserve(start)

    assert record.start.tzinfo is UTC
    assert record.start == datetime(2026, 10, 25, 1, 15, tzinfo=UTC)
    assert record.end - record.start == INTERVAL
    assert record.end == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


@pytest.mark.parametrize("kind", ["imbalance", "reserve"])
def test_non_utc_aware_start_is_rejected_not_normalised(kind: str) -> None:
    # An offset-zero zone is normalised; any other offset must raise.
    start = datetime(2026, 1, 15, 12, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))

    with pytest.raises(ValueError, match="must be a UTC datetime"):
        replace(price({}), start=start) if kind == "imbalance" else reserve(start)


@pytest.mark.parametrize("kind", ["imbalance", "reserve"])
def test_naive_start_is_rejected(kind: str) -> None:
    start = datetime(2026, 1, 15, 12, 0)

    with pytest.raises(ValueError, match="must be a UTC datetime"):
        replace(price({}), start=start) if kind == "imbalance" else reserve(start)


def test_reserve_capacity_raw_top_level_is_read_only() -> None:
    record = reserve(START)
    with pytest.raises(TypeError):
        record.raw["a"] = 2  # type: ignore[index]


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str:
        return "NONE"


def test_to_utc_rejects_a_datetime_python_treats_as_naive() -> None:
    # A tzinfo whose utcoffset() is None makes the datetime naive per the
    # datetime docs; to_utc must reject it, not convert it as local time.
    with pytest.raises(ValueError, match="timezone-aware"):
        to_utc(datetime(2026, 1, 15, 12, 0, tzinfo=_NoOffset()), "start")


def test_to_utc_normalises_non_utc_zone() -> None:
    stamp = to_utc(datetime(2026, 1, 15, 12, 0, tzinfo=ZoneInfo("Europe/Copenhagen")), "start")
    assert stamp.tzinfo is UTC
    assert stamp == datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
