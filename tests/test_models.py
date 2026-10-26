import pickle
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fakes import (
    eds_client,
    eds_server,
    esett_client,
    esett_server,
    http_client,
)
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
from nordic_balancing.models import (
    _MAX_YEAR,
    _copy_raw,
    _owned_imbalance_price,
    _owned_reserve_capacity,
    to_utc,
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


def test_owned_constructor_sets_every_field_like_the_public_one() -> None:
    raw = {"319": {"value": 1}}
    public = replace(
        price(raw),
        zone=BiddingZone.SE3,
        imbalance_price_eur=1.5,
        mfrr_up_price_eur=2.5,
        mfrr_down_price_eur=-3.5,
        dominating_direction=Direction.UP,
        resolution=timedelta(hours=1),
    )
    owned = _owned_imbalance_price(
        start=START,
        zone=BiddingZone.SE3,
        imbalance_price_eur=1.5,
        mfrr_up_price_eur=2.5,
        mfrr_down_price_eur=-3.5,
        dominating_direction=Direction.UP,
        source="test",
        resolution=timedelta(hours=1),
        raw=raw,
    )
    assert asdict(owned) == asdict(public)
    assert type(owned.raw) is type(public.raw)
    assert owned.raw._data is raw  # type: ignore[attr-defined]  # handed over, not copied
    copied = replace(owned, start=START + INTERVAL)
    assert copied.raw["319"] is not raw["319"]


def test_owned_capacity_constructor_sets_every_field_like_the_public_one() -> None:
    raw: dict[str, Any] = {"nested": {"value": 1}}
    fields: dict[str, Any] = {
        "start": START,
        "zone": BiddingZone.NO1,
        "product": ReserveProduct.FCR_N,
        "direction": Direction.NONE,
        "price_eur_per_mw": 4.5,
        "volume_mw": None,
        "source": "test",
        "resolution": timedelta(hours=1),
    }
    owned = _owned_reserve_capacity(**fields, raw=raw)
    public = ReserveCapacity(**fields, raw=raw)
    assert asdict(owned) == asdict(public)
    assert type(owned.raw) is type(public.raw)
    assert owned.raw._data is raw  # type: ignore[attr-defined]  # handed over, not copied
    assert replace(owned, start=START + INTERVAL).raw["nested"] is not raw["nested"]


def test_raw_top_level_is_read_only() -> None:
    record = price({"a": 1})
    with pytest.raises(TypeError):
        record.raw["a"] = 2  # type: ignore[index]


def test_raw_preserves_json_like_types() -> None:
    raw = {"t": (1, 2), "l": [1], "d": {"k": None}, "n": 1.5, "s": "x"}
    assert dict(price(raw).raw) == raw


def test_raw_is_a_full_mapping_with_len_and_repr() -> None:
    record = price({"a": 1, "b": [2]})
    assert len(record.raw) == 2
    assert list(record.raw) == ["a", "b"]
    assert repr(record.raw) == repr({"a": 1, "b": [2]})


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


def test_raw_deep_copies_scalar_subclasses_with_state() -> None:
    class Tagged(str):
        tags: list[str]

    value = Tagged("x")
    value.tags = ["a"]
    record = price({"value": value})
    value.tags.append("b")
    assert record.raw["value"] == "x"
    assert record.raw["value"] is not value
    assert record.raw["value"].tags == ["a"]


def test_raw_normalises_dict_subclasses_and_other_mappings() -> None:
    class SubDict(dict[str, Any]):
        pass

    raw: dict[str, Any] = {
        "sub": SubDict({"v": [1]}),
        "proxy": MappingProxyType({"w": [2]}),
    }
    record = price(raw)
    assert type(record.raw["sub"]) is dict
    assert type(record.raw["proxy"]) is dict
    record.raw["sub"]["v"].append(9)
    assert raw["sub"]["v"] == [1]


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


def test_record_pickles_deepcopies_and_asdicts_with_detached_read_only_raw() -> None:
    record = price({"mba": "FI", "nested": {"x": 1}})

    restored = pickle.loads(pickle.dumps(record))
    assert restored == record
    assert dict(restored.raw) == dict(record.raw)
    with pytest.raises(TypeError):
        restored.raw["mba"] = "DK1"

    copied = deepcopy(record)
    assert copied == record
    copied.raw["nested"]["x"] = 99
    assert record.raw["nested"]["x"] == 1

    data = asdict(record)
    assert data["zone"] == BiddingZone.FI
    assert dict(data["raw"]) == {"mba": "FI", "nested": {"x": 1}}
    data["raw"]["nested"]["x"] = 99
    assert record.raw["nested"]["x"] == 1


def test_reserve_capacity_pickles_too() -> None:
    record = ReserveCapacity(
        start=START,
        zone=BiddingZone.SE3,
        product=ReserveProduct.MFRR,
        direction=Direction.UP,
        price_eur_per_mw=1.0,
        volume_mw=2.0,
        source="test",
        resolution=INTERVAL,
        raw={"v": 1},
    )
    restored = pickle.loads(pickle.dumps(record))
    assert restored == record
    assert dict(restored.raw) == {"v": 1}


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


@pytest.mark.parametrize("kind", ["imbalance", "reserve"])
def test_non_datetime_start_is_rejected(kind: str) -> None:
    # A date is the plausible mistake (date.today()); it must fail as a wrong
    # type, not leak AttributeError from the tzinfo check.
    with pytest.raises(TypeError, match="start must be a datetime"):
        (
            replace(price({}), start=date(2026, 1, 15))  # type: ignore[arg-type]
            if kind == "imbalance"
            else reserve(date(2026, 1, 15))  # type: ignore[arg-type]
        )


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


@pytest.mark.parametrize("value", [date(2026, 1, 15), "2026-01-15", None, 42])
def test_to_utc_rejects_non_datetimes(value: object) -> None:
    with pytest.raises(TypeError, match="start must be a datetime"):
        to_utc(value, "start")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        # UTC conversion of an offset-zone bound near the datetime limits
        # overflows; the caller must get a ValueError, not a raw OverflowError.
        datetime.max.replace(tzinfo=timezone(timedelta(hours=-1))),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_to_utc_wraps_utc_conversion_overflow(value: datetime) -> None:
    with pytest.raises(ValueError, match="cannot be represented in UTC"):
        to_utc(value, "end")


@pytest.mark.parametrize(
    "value",
    [
        datetime.max.replace(tzinfo=UTC),
        datetime(9998, 1, 1, tzinfo=UTC),
        datetime(1, 6, 1, tzinfo=UTC),
    ],
)
def test_to_utc_rejects_years_where_window_arithmetic_overflows(value: datetime) -> None:
    with pytest.raises(ValueError, match="between years 2 and 9997"):
        to_utc(value, "end")


def test_to_utc_accepts_the_edges_of_the_supported_range() -> None:
    assert to_utc(datetime(9997, 12, 31, 23, 45, tzinfo=UTC), "end").year == 9997
    assert to_utc(datetime(2, 1, 1, tzinfo=UTC), "start").year == 2


def test_query_window_near_datetime_max_raises_value_error_not_overflow() -> None:
    from nordic_balancing import EnergiDataServiceClient, EntsoeClient, ESettClient

    for client in (EnergiDataServiceClient(), ESettClient(), EntsoeClient(api_key="test")):
        with client, pytest.raises(ValueError, match="between years"):
            client.imbalance_prices(START, datetime.max.replace(tzinfo=UTC))


def test_entsoe_chunking_at_the_last_supported_year_does_not_overflow() -> None:
    import httpx

    from nordic_balancing import EntsoeClient, NordicBalancingError

    # ENTSO-E chunks by up to 366 days; the year bound must leave room for that, so
    # the request is built and the (mocked) server's answer decides the outcome.
    http = http_client(lambda _: httpx.Response(400))
    client = EntsoeClient(api_key="test", http=http, max_retries=0)
    with client, pytest.raises(NordicBalancingError, match="HTTP 400 without an acknowledgement"):
        client.imbalance_prices(
            datetime(_MAX_YEAR, 12, 31, tzinfo=UTC),
            datetime(_MAX_YEAR, 12, 31, 23, 45, tzinfo=UTC),
        )


def test_to_utc_returns_a_plain_datetime_without_sub_microseconds() -> None:
    pd = pytest.importorskip("pandas")
    # pandas.Timestamp is a datetime subclass carrying nanoseconds; 15-minute
    # window arithmetic works at microsecond resolution, so the finer part is
    # dropped, not rounded.
    stamp = to_utc(pd.Timestamp("2026-01-15 12:15:00.000000001Z"), "start")
    assert type(stamp) is datetime
    assert stamp.tzinfo is UTC
    assert stamp == datetime(2026, 1, 15, 12, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "query",
    [
        lambda start, end: esett_client(esett_server).imbalance_prices(
            start, end, [BiddingZone.SE3]
        ),
        lambda start, end: eds_client(eds_server).imbalance_prices(start, end),
    ],
    ids=["esett", "eds"],
)
def test_sub_microsecond_window_edges_return_the_same_records(query: Any) -> None:
    pd = pytest.importorskip("pandas")
    nanosecond = pd.Timedelta(1, "ns")
    start = pd.Timestamp("2026-01-15 12:00Z")
    end = pd.Timestamp("2026-01-15 12:30Z")

    shifted = query(start + nanosecond, end + nanosecond)
    exact = query(start.to_pydatetime(), end.to_pydatetime())

    assert [record.start for record in shifted] == [record.start for record in exact]
    assert shifted == exact
    assert len(shifted) > 0
