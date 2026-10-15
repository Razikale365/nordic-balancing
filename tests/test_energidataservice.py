import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EnergiDataServiceClient,
    RateLimitError,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
)

# Shaped like real ImbalancePrice records (fetched 2026-10-09).
PUBLISHED: dict[str, Any] = {
    "TimeUTC": "2026-10-09T02:30:00",
    "PriceArea": "DK2",
    "ImbalancePriceEUR": 79.27,
    "ImbalancePriceDKK": 592.46,
    "SpotPriceEUR": 79.27,
    "DominatingDirection": 0,
    "SatisfiedDemand": 0.0,
    "aFRRVWAUpEUR": 0.0,
    "aFRRVWADownEUR": -3.53,
    "mFRRMarginalPriceUpEUR": 79.27,
    "mFRRMarginalPriceDownEUR": 40.14,
}
NOT_YET_PUBLISHED: dict[str, Any] = {
    **PUBLISHED,
    "TimeUTC": "2026-10-09T02:45:00",
    "PriceArea": "DK1",
    "ImbalancePriceEUR": None,
    "ImbalancePriceDKK": None,
    "DominatingDirection": -1,
    "SatisfiedDemand": -95.0,
    "mFRRMarginalPriceUpEUR": None,
    "mFRRMarginalPriceDownEUR": None,
}

START = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)
END = START + timedelta(minutes=30)

Handler = Callable[[httpx.Request], httpx.Response]

CAPACITY: dict[str, Any] = {
    "TimeUTC": "2026-10-09T02:00:00",
    "TimeDK": "2026-10-09T04:00:00",
    "PriceArea": "DK1",
    "UpDemandMW": 20.0,
    "UpProcuredMW": 30.0,
    "UpPriceEUR": -2.5,
    "UpPriceDKK": -18.65,
    "DownDemandMW": 10.0,
    "DownProcuredMW": 0.0,
    "DownPriceEUR": 5.0,
    "DownPriceDKK": 37.3,
}
CAPACITY_NUMBERS = tuple(key for key in CAPACITY if key.startswith(("Up", "Down")))
HOUR_START = START.replace(minute=0)


@pytest.mark.parametrize("product", list(ReserveProduct))
def test_capacity_expands_both_directions_and_preserves_mapping(product: ReserveProduct) -> None:
    # Given a row whose procurement differs from demand, including zero and negatives.
    eds = client(lambda _: page([CAPACITY]))
    # When requesting one whole hour.
    result = eds.capacity_market(product, HOUR_START, END, zones=[BiddingZone.DK1])
    # Then values are levels repeated on quarters, with source resolution and raw intact.
    assert len(result) == 8
    assert [(r.start, r.zone, r.direction) for r in result] == [
        (HOUR_START + q * INTERVAL, BiddingZone.DK1, direction)
        for q in range(4)
        for direction in (Direction.DOWN, Direction.UP)
    ]
    for record in result:
        assert isinstance(record, ReserveCapacity)
        assert record.product is product
        assert record.resolution == timedelta(hours=1)
        assert record.source == "energidataservice"
        assert record.start.tzinfo is UTC
        assert record.end == record.start + INTERVAL
        assert record.raw == CAPACITY
    assert (result[0].price_eur_per_mw, result[0].volume_mw) == (5.0, 0.0)
    assert (result[1].price_eur_per_mw, result[1].volume_mw) == (-2.5, 30.0)


@pytest.mark.parametrize("zone", list(BiddingZone))
def test_capacity_afrr_covers_every_nordic_zone(zone: BiddingZone) -> None:
    # Given one synthetic aFRR row for a Nordic zone.
    row = {**CAPACITY, "PriceArea": zone.value}
    # When fetching that zone through the public method.
    result = client(lambda _: page([row])).capacity_market(
        ReserveProduct.AFRR, HOUR_START, END, zones=[zone]
    )
    # Then all expanded records retain the zone.
    assert len(result) == 8
    assert {r.zone for r in result} == {zone}


def page(records: list[dict[str, Any]], total: int | None = None) -> httpx.Response:
    body = {"total": len(records) if total is None else total, "records": records}
    return httpx.Response(200, json=body)


def client(handler: Handler, sleeps: list[float] | None = None) -> EnergiDataServiceClient:
    recorded = sleeps if sleeps is not None else []
    return EnergiDataServiceClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=recorded.append,
    )


def test_parses_published_and_unpublished_intervals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return page([NOT_YET_PUBLISHED, PUBLISHED])

    prices = client(handler).imbalance_prices(START, END)

    assert [(p.start, p.zone) for p in prices] == [
        (START, BiddingZone.DK2),
        (START + timedelta(minutes=15), BiddingZone.DK1),
    ]
    published, pending = prices
    assert published.imbalance_price_eur == 79.27
    assert published.dominating_direction is Direction.NONE
    assert published.end == START + timedelta(minutes=15)
    assert published.source == "energidataservice"
    assert pending.imbalance_price_eur is None
    assert pending.dominating_direction is Direction.DOWN


def test_sends_utc_window_and_zone_filter() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page([])

    copenhagen = timezone(timedelta(hours=2))
    client(handler).imbalance_prices(
        START.astimezone(copenhagen), END.astimezone(copenhagen), [BiddingZone.DK1]
    )

    params = seen[0].url.params
    assert seen[0].url.path == "/dataset/ImbalancePrice"
    assert params["start"] == "2026-10-09T02:30"
    assert params["end"] == "2026-10-09T03:00"
    assert params["timezone"] == "UTC"
    assert json.loads(params["filter"]) == {"PriceArea": ["DK1"]}


def test_pages_until_total_is_reached() -> None:
    offsets: list[str] = []
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["limit"])
        offset = int(request.url.params["offset"])
        offsets.append(request.url.params["offset"])
        # A full page each time; the last page carries the single extra row.
        records = [
            {
                **PUBLISHED,
                "PriceArea": "DK1",
                "TimeUTC": (start + (offset + i) * INTERVAL).strftime("%Y-%m-%dT%H:%M:%S"),
            }
            for i in range(min(limit, limit + 1 - offset))
        ]
        return page(records, total=limit + 1)

    prices = client(handler).imbalance_prices(start, start + timedelta(days=200), [BiddingZone.DK1])

    assert offsets == ["0", "10000"]
    assert len(prices) == 10001


@pytest.mark.parametrize(
    "body",
    [
        {"records": []},
        {"total": None, "records": []},
        {"total": True, "records": []},
        {"total": "2", "records": []},
        {"total": 2.5, "records": []},
        {"total": -1, "records": []},
    ],
)
def test_rejects_missing_or_invalid_total(body: dict[str, Any]) -> None:
    with pytest.raises(SourceError, match="invalid total"):
        client(lambda _: httpx.Response(200, json=body)).imbalance_prices(START, END)


@pytest.mark.parametrize("records", [[], [PUBLISHED]])
def test_rejects_short_or_empty_page_before_total(records: list[dict[str, Any]]) -> None:
    with pytest.raises(SourceError, match="before total"):
        client(lambda _: page(records, total=5)).imbalance_prices(START, END)


def test_rejects_row_count_exceeding_total() -> None:
    with pytest.raises(SourceError, match="exceeds total"):
        client(lambda _: page([PUBLISHED, NOT_YET_PUBLISHED], total=1)).imbalance_prices(START, END)


def test_retries_rate_limit_honouring_retry_after() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429),
            page([PUBLISHED]),
        ]
    )
    sleeps: list[float] = []

    prices = client(lambda _: next(responses), sleeps).imbalance_prices(START, END)

    assert sleeps == [7.0, 10.0]
    assert len(prices) == 1


def test_gives_up_after_max_retries() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitError, match="retry after 3 s"):
        client(handler, sleeps).imbalance_prices(START, END)
    assert sleeps == [3.0, 3.0, 3.0]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"TimeUTC": "2026-10-09T02:40:00"}, "15-minute boundary"),
        ({"TimeUTC": None}, "TimeUTC is missing"),
        ({"PriceArea": "SE3"}, "unknown PriceArea"),
        ({"ImbalancePriceEUR": "79.27"}, "not a number"),
        ({"DominatingDirection": 2}, "unknown DominatingDirection"),
    ],
)
def test_rejects_malformed_records(override: dict[str, Any], message: str) -> None:
    record = {**PUBLISHED, **override}

    with pytest.raises(SourceError, match=message):
        client(lambda _: page([record])).imbalance_prices(START, END)


@pytest.mark.parametrize("record", [1, None, "x", []])
def test_rejects_record_that_is_not_an_object(record: object) -> None:
    body = {"total": 1, "records": [record]}

    with pytest.raises(SourceError, match="not a JSON object"):
        client(lambda _: httpx.Response(200, json=body)).imbalance_prices(START, END)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_numbers(value: str) -> None:
    record = {**PUBLISHED, "ImbalancePriceEUR": json.loads(value)}
    body = json.dumps({"total": 1, "records": [record]})
    with pytest.raises(SourceError, match="not finite"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(START, END)


def test_rejects_duplicate_zone_and_interval() -> None:
    duplicate = {**PUBLISHED, "ImbalancePriceEUR": 99.0}
    with pytest.raises(SourceError, match="duplicate"):
        client(lambda _: page([PUBLISHED, duplicate])).imbalance_prices(START, END)


def test_rejects_unrequested_zone() -> None:
    with pytest.raises(SourceError, match="unrequested PriceArea"):
        client(lambda _: page([PUBLISHED])).imbalance_prices(START, END, [BiddingZone.DK1])


@pytest.mark.parametrize("time_utc", ["2026-10-09T02:00:00", "2026-10-09T03:00:00"])
def test_rejects_records_outside_the_query_window(time_utc: str) -> None:
    record = {**PUBLISHED, "TimeUTC": time_utc}
    with pytest.raises(SourceError, match="outside requested window"):
        client(lambda _: page([record])).imbalance_prices(START, END)


def test_sub_minute_window_bounds_are_rounded_outwards_then_filtered() -> None:
    seen: list[httpx.Request] = []
    before_start = {**PUBLISHED, "TimeUTC": "2026-10-09T02:30:00", "PriceArea": "DK1"}
    last_interval = {
        **PUBLISHED,
        "TimeUTC": "2026-10-09T02:45:00",
        "PriceArea": "DK2",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page([before_start, last_interval])

    prices = client(handler).imbalance_prices(
        START + timedelta(seconds=30), END - timedelta(seconds=30)
    )

    params = seen[0].url.params
    assert params["start"] == "2026-10-09T02:30"
    assert params["end"] == "2026-10-09T03:00"
    assert [p.start for p in prices] == [START + INTERVAL]


def test_http_error_is_a_source_error() -> None:
    with pytest.raises(SourceError, match="HTTP 400"):
        client(lambda _: httpx.Response(400, text="bad filter")).imbalance_prices(START, END)


@pytest.mark.parametrize(
    ("start", "end", "zones", "message"),
    [
        (START.replace(tzinfo=None), END, None, "timezone-aware"),
        (END, START, None, "end must be after start"),
        (START, END, [], "zones must not be empty"),
    ],
)
def test_rejects_bad_arguments(
    start: datetime, end: datetime, zones: list[BiddingZone] | None, message: str
) -> None:
    c = client(lambda _: page([]))
    with pytest.raises(ValueError, match=message):
        if zones is None:
            c.imbalance_prices(start, end)
        else:
            c.imbalance_prices(start, end, zones)


def test_closes_only_its_own_http_client() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _: page([])))
    with EnergiDataServiceClient(http):
        pass
    assert not http.is_closed


@pytest.mark.live
def test_live_one_hour_of_dk_imbalance_prices() -> None:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    with EnergiDataServiceClient() as eds:
        prices = eds.imbalance_prices(start, start + timedelta(hours=1))

    assert len(prices) == 8  # 4 intervals x DK1, DK2
    assert prices[0].start == start
    assert {p.zone for p in prices} == {BiddingZone.DK1, BiddingZone.DK2}
    assert all(p.imbalance_price_eur is not None for p in prices)


@pytest.mark.parametrize("zone", [BiddingZone.FI, BiddingZone.NO1, BiddingZone.SE3])
def test_rejects_non_danish_zone_arguments(zone: BiddingZone) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("non-Danish zones must not send an HTTP request")

    with pytest.raises(ValueError, match="only DK1 and DK2"):
        client(handler).imbalance_prices(START, END, [zone])


def test_preserves_resolution_raw_and_renamed_mfrr_fields() -> None:
    price = client(lambda _: page([PUBLISHED])).imbalance_prices(START, END)[0]
    assert price.resolution == timedelta(minutes=15)
    assert price.raw == PUBLISHED
    assert price.mfrr_up_price_eur == 79.27
    assert price.mfrr_down_price_eur == 40.14
    with pytest.raises(TypeError):
        price.raw["PriceArea"] = "DK1"  # type: ignore[index]


def test_transport_error_becomes_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    with pytest.raises(SourceError, match="request failed"):
        EnergiDataServiceClient(
            httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None
        ).imbalance_prices(START, END)


@pytest.mark.parametrize("value", [0.6, -0.4, 1.9, -1.5])
def test_rejects_fractional_dominating_direction(value: float) -> None:
    # int() would silently truncate these to a valid direction.
    record = {**PUBLISHED, "DominatingDirection": value}
    with pytest.raises(SourceError, match="unknown DominatingDirection"):
        client(lambda _: page([record])).imbalance_prices(START, END)


@pytest.mark.parametrize(
    ("value", "expected"), [(-1, Direction.DOWN), (0, Direction.NONE), (1.0, Direction.UP)]
)
def test_accepts_exact_dominating_directions(value: float, expected: Direction) -> None:
    record = {**PUBLISHED, "DominatingDirection": value}
    price = client(lambda _: page([record])).imbalance_prices(START, END)[0]
    assert price.dominating_direction is expected


# --- Audit 2026-10-09 (baseline v0.1.0a3) -------------------------------------------


@pytest.mark.parametrize(
    "time_utc",
    ["2026-10-09T02:30:00.5", "2026-10-09T02:30:00.000001", "2026-10-09T02:30:30"],
)
def test_rejects_sub_minute_offsets_from_the_quarter_hour_grid(time_utc: str) -> None:
    record = {**PUBLISHED, "TimeUTC": time_utc}
    with pytest.raises(SourceError, match="15-minute boundary"):
        client(lambda _: page([record])).imbalance_prices(START, END)


@pytest.mark.parametrize("retries", [-1, True, 1.5, "3"])
def test_rejects_bad_retry_budget(retries: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        EnergiDataServiceClient(max_retries=retries)


@pytest.mark.parametrize("status", [502, 504])
def test_retries_transient_gateway_errors(status: int) -> None:
    responses = iter([httpx.Response(status), page([PUBLISHED])])
    sleeps: list[float] = []
    prices = client(lambda _: next(responses), sleeps).imbalance_prices(START, END)
    assert len(prices) == 1
    assert sleeps == [10.0]


@pytest.mark.parametrize("status", [502, 504])
def test_exhausted_gateway_errors_are_source_errors_not_rate_limits(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    with pytest.raises(SourceError, match=f"HTTP {status}"):
        client(handler).imbalance_prices(START, END)
    assert len(calls) == 4


def test_retries_transport_errors_then_gives_up_as_source_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectTimeout("timed out", request=request)

    sleeps: list[float] = []
    with pytest.raises(SourceError, match="request failed"):
        client(handler, sleeps).imbalance_prices(START, END)
    assert len(calls) == 4
    assert sleeps == [10.0] * 3


def test_transport_error_then_success() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadError("reset", request=request)
        return page([PUBLISHED])

    assert len(client(handler).imbalance_prices(START, END)) == 1


@pytest.mark.parametrize("header", ["inf", "nan", "-inf", "1e400"])
def test_non_finite_retry_after_uses_the_default_delay(header: str) -> None:
    responses = iter([httpx.Response(429, headers={"Retry-After": header}), page([PUBLISHED])])
    sleeps: list[float] = []
    client(lambda _: next(responses), sleeps).imbalance_prices(START, END)
    assert sleeps == [10.0]


def test_retry_after_beyond_the_cap_raises_without_sleeping() -> None:
    sleeps: list[float] = []
    with pytest.raises(RateLimitError) as caught:
        client(
            lambda _: httpx.Response(429, headers={"Retry-After": "3600"}), sleeps
        ).imbalance_prices(START, END)
    assert caught.value.retry_after == 3600.0
    assert sleeps == []


def test_deterministic_transport_errors_are_not_retried() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.UnsupportedProtocol("bad scheme", request=request)

    sleeps: list[float] = []
    with pytest.raises(SourceError, match="request failed"):
        client(handler, sleeps).imbalance_prices(START, END)
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("first_total", "second_total", "second_count"),
    [
        (10_002, 10_000, 0),
        (10_001, 10_002, 2),
    ],
)
def test_rejects_total_changing_across_pages(
    first_total: int, second_total: int, second_count: int
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def records_at(offset: int, count: int) -> list[dict[str, Any]]:
        return [
            {
                **PUBLISHED,
                "PriceArea": "DK1",
                "TimeUTC": (start + (offset + i) * INTERVAL).strftime("%Y-%m-%dT%H:%M:%S"),
            }
            for i in range(count)
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        if offset == 0:
            return page(records_at(0, 10_000), total=first_total)
        return page(records_at(offset, second_count), total=second_total)

    with pytest.raises(SourceError, match="changed from"):
        client(handler).imbalance_prices(start, start + timedelta(days=120), [BiddingZone.DK1])


@pytest.mark.parametrize(
    "time_utc",
    [
        "2026-10-09",
        "20261009",
        "2026-10-09T02:30:30+00:00:30",
        "2026-10-09T04:30:00+02:00",
        "2026-10-09T02:30:00-00:00",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:45:00-01:00",
    ],
)
def test_rejects_bare_date_non_utc_offset_and_overflow_timestamps(time_utc: str) -> None:
    start = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
    record = {**PUBLISHED, "TimeUTC": time_utc}
    with pytest.raises(SourceError):
        client(lambda _: page([record])).imbalance_prices(start, start + timedelta(hours=3))
    valid_records = [
        {**PUBLISHED, "TimeUTC": "2026-10-09T02:30:00Z", "PriceArea": "DK1"},
        {**PUBLISHED, "TimeUTC": "2026-10-09T02:30:00+00:00", "PriceArea": "DK2"},
    ]
    prices = client(lambda _: page(valid_records)).imbalance_prices(START, END)
    assert [p.start for p in prices] == [START, START]


@pytest.mark.parametrize(
    ("time_utc", "expected_start"),
    [
        ("20261009T023000", START),
        ("2026-10-09T02", datetime(2026, 10, 9, 2, 0, tzinfo=UTC)),
    ],
)
def test_preserves_compact_and_hour_only_naive_timestamps(
    time_utc: str, expected_start: datetime
) -> None:
    window_start = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)
    record = {**PUBLISHED, "TimeUTC": time_utc}
    prices = client(lambda _: page([record])).imbalance_prices(window_start, END)
    assert [p.start for p in prices] == [expected_start]


@pytest.mark.parametrize(
    ("product", "dataset"),
    [(ReserveProduct.AFRR, "AfrrReservesNordic"), (ReserveProduct.MFRR, "mFRRCapacityMarket")],
)
def test_capacity_query_uses_dataset_defaults_columns_and_utc(
    product: ReserveProduct, dataset: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page([])

    local = timezone(timedelta(hours=2))
    result = client(handler).capacity_market(
        product, START.astimezone(local), END.astimezone(local)
    )
    assert result == []
    assert seen[0].url.path == f"/dataset/{dataset}"
    params = seen[0].url.params
    assert params["start"] == "2026-10-09T02:00"
    assert params["end"] == "2026-10-09T03:00"
    assert params["timezone"] == "UTC"
    assert json.loads(params["filter"]) == {"PriceArea": ["DK1", "DK2"]}
    assert params["columns"].split(",") == list(CAPACITY)
    assert params["sort"] == "TimeUTC asc,PriceArea asc"
    assert params["offset"] == "0"
    assert params["limit"] == "10000"


def test_capacity_rounds_outward_and_trims_at_subsecond_precision() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page([CAPACITY, {**CAPACITY, "TimeUTC": "2026-10-09T03:00:00"}])

    result = client(handler).capacity_market(
        ReserveProduct.AFRR,
        HOUR_START + INTERVAL + timedelta(microseconds=1),
        END + timedelta(microseconds=1),
        zones=[BiddingZone.DK1],
    )
    assert seen[0].url.params["start"] == "2026-10-09T02:00"
    assert seen[0].url.params["end"] == "2026-10-09T03:01"
    assert [r.start for r in result] == [HOUR_START + q * INTERVAL for q in (2, 2, 3, 3, 4, 4)]


def test_capacity_sorts_rows_and_accepts_deduplicated_zone_generator() -> None:
    rows = [{**CAPACITY, "PriceArea": "DK2"}, CAPACITY]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page(rows)

    result = client(handler).capacity_market(
        ReserveProduct.MFRR,
        HOUR_START,
        END,
        zones=(z for z in (BiddingZone.DK2, BiddingZone.DK1, BiddingZone.DK2)),
    )
    assert json.loads(seen[0].url.params["filter"]) == {"PriceArea": ["DK1", "DK2"]}
    assert [(r.start, r.zone, r.direction) for r in result] == [
        (HOUR_START + q * INTERVAL, zone, direction)
        for q in range(4)
        for zone in (BiddingZone.DK1, BiddingZone.DK2)
        for direction in (Direction.DOWN, Direction.UP)
    ]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"TimeUTC": None}, "TimeUTC is missing"),
        ({"TimeUTC": "2026-10-09"}, "unparseable TimeUTC"),
        ({"TimeUTC": "invalid"}, "unparseable TimeUTC"),
        ({"TimeUTC": "2026-10-09T02:15:00"}, "not on the hour"),
        ({"TimeUTC": "2026-10-09T02:00:00.000001"}, "15-minute boundary"),
        ({"TimeUTC": "2026-10-09T02:00:01"}, "15-minute boundary"),
        ({"TimeUTC": "2026-10-09T02:00:00+02:00"}, "must be UTC"),
        ({"TimeUTC": "2026-10-09T02:00:00-00:00"}, "must be UTC"),
        ({"TimeUTC": "2026-10-09T01:00:00"}, "outside requested window"),
        ({"TimeUTC": "2026-10-09T03:00:00"}, "outside requested window"),
        ({"PriceArea": "SE3"}, "unrequested PriceArea"),
        ({"PriceArea": "unknown"}, "unrequested PriceArea"),
        ({"PriceArea": None}, "unrequested PriceArea"),
        ({"PriceArea": []}, "unrequested PriceArea"),
    ],
)
def test_capacity_rejects_malformed_rows(override: dict[str, Any], message: str) -> None:
    with pytest.raises(SourceError, match=message):
        client(lambda _: page([{**CAPACITY, **override}])).capacity_market(
            ReserveProduct.AFRR, START, END
        )


@pytest.mark.parametrize("column", list(CAPACITY))
def test_capacity_rejects_missing_columns(column: str) -> None:
    row = {key: value for key, value in CAPACITY.items() if key != column}
    with pytest.raises(SourceError, match=f"missing capacity columns.*{column}"):
        client(lambda _: page([row])).capacity_market(ReserveProduct.AFRR, HOUR_START, END)


@pytest.mark.parametrize("column", CAPACITY_NUMBERS)
@pytest.mark.parametrize(
    "value", [True, "1.0", [], {}, float("nan"), float("inf"), -float("inf"), 10**400]
)
def test_capacity_rejects_invalid_numbers_including_raw_columns(column: str, value: Any) -> None:
    body = json.dumps({"total": 1, "records": [{**CAPACITY, column: value}]})
    with pytest.raises(SourceError, match=f"{column} is not (a number|finite)"):
        client(lambda _: httpx.Response(200, text=body)).capacity_market(
            ReserveProduct.AFRR, HOUR_START, END
        )


def test_capacity_accepts_all_null_numbers() -> None:
    row = {**CAPACITY, **dict.fromkeys(CAPACITY_NUMBERS)}
    result = client(lambda _: page([row])).capacity_market(ReserveProduct.MFRR, HOUR_START, END)
    assert len(result) == 8
    assert all(r.price_eur_per_mw is None and r.volume_mw is None for r in result)
    assert all(r.raw[column] is None for r in result for column in CAPACITY_NUMBERS)


def test_capacity_accepts_negative_numbers_without_sign_checks() -> None:
    row = {**CAPACITY, **dict.fromkeys(CAPACITY_NUMBERS, -1.25)}
    result = client(lambda _: page([row])).capacity_market(ReserveProduct.MFRR, HOUR_START, END)
    assert len(result) == 8
    assert all(r.price_eur_per_mw == -1.25 and r.volume_mw == -1.25 for r in result)


@pytest.mark.parametrize("suffix", ["", "Z", "+00:00"])
def test_capacity_accepts_naive_utc_and_explicit_utc_timestamps(suffix: str) -> None:
    row = {**CAPACITY, "TimeUTC": CAPACITY["TimeUTC"] + suffix}
    result = client(lambda _: page([row])).capacity_market(ReserveProduct.AFRR, HOUR_START, END)
    assert len(result) == 8
    assert result[0].start == HOUR_START


@pytest.mark.parametrize("record", [None, [], "bad", 1])
def test_capacity_rejects_non_object_record(record: Any) -> None:
    body = {"total": 1, "records": [record]}
    with pytest.raises(SourceError, match="not a JSON object"):
        client(lambda _: httpx.Response(200, json=body)).capacity_market(
            ReserveProduct.AFRR, HOUR_START, END
        )


def test_capacity_rejects_duplicate_hour_zone_even_when_trimmed_out() -> None:
    with pytest.raises(SourceError, match="duplicate record"):
        client(lambda _: page([CAPACITY, CAPACITY])).capacity_market(
            ReserveProduct.AFRR, END - timedelta(seconds=1), END
        )


def test_capacity_raw_is_private_to_each_direction_and_quarter() -> None:
    row = {**CAPACITY, "metadata": {"values": [1]}}
    result = client(lambda _: page([row])).capacity_market(ReserveProduct.AFRR, HOUR_START, END)
    result[0].raw["metadata"]["values"].append(2)
    assert all(r.raw["metadata"]["values"] == [1] for r in result[1:])
    assert row["metadata"]["values"] == [1]


@pytest.mark.parametrize(
    ("product", "start", "end", "zones", "message"),
    [
        ("aFRR", START, END, [BiddingZone.DK1], "ReserveProduct"),
        (ReserveProduct.AFRR, START.replace(tzinfo=None), END, [BiddingZone.DK1], "timezone-aware"),
        (ReserveProduct.AFRR, START, END.replace(tzinfo=None), [BiddingZone.DK1], "timezone-aware"),
        (ReserveProduct.AFRR, END, START, [BiddingZone.DK1], "end must be after start"),
        (ReserveProduct.AFRR, START, START, [BiddingZone.DK1], "end must be after start"),
        (ReserveProduct.AFRR, START, END, [], "zones must not be empty"),
        (ReserveProduct.AFRR, START, END, ["DK1"], "BiddingZone"),
        (ReserveProduct.AFRR, START, END, [None], "BiddingZone"),
        *[
            (ReserveProduct.MFRR, START, END, [zone], "only DK1 and DK2")
            for zone in BiddingZone
            if zone not in (BiddingZone.DK1, BiddingZone.DK2)
        ],
    ],
)
def test_capacity_rejects_bad_arguments_before_http(
    product: Any, start: datetime, end: datetime, zones: Any, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("bad arguments must not send an HTTP request")

    with pytest.raises(ValueError, match=message):
        client(handler).capacity_market(product, start, end, zones=zones)


def test_capacity_pages_until_total_and_rejects_cross_page_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nordic_balancing.sources.energidataservice.PAGE_SIZE", 1)
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = request.url.params["offset"]
        offsets.append(offset)
        return page([{**CAPACITY, "PriceArea": "DK1" if offset == "0" else "DK2"}], total=2)

    result = client(handler).capacity_market(ReserveProduct.MFRR, HOUR_START, END)
    assert len(result) == 16
    assert offsets == ["0", "1"]
    with pytest.raises(SourceError, match="duplicate record"):
        client(lambda _: page([CAPACITY], total=2)).capacity_market(
            ReserveProduct.MFRR, HOUR_START, END
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"records": []}, "invalid total"),
        ({"total": True, "records": []}, "invalid total"),
        ({"total": 0}, "no 'records' list"),
        ({"total": 2, "records": [CAPACITY]}, "before total"),
        ({"total": 1, "records": []}, "before total"),
        ({"total": 0, "records": [CAPACITY]}, "exceeds total"),
    ],
)
def test_capacity_reuses_pagination_guards(body: dict[str, Any], message: str) -> None:
    with pytest.raises(SourceError, match=message):
        client(lambda _: httpx.Response(200, json=body)).capacity_market(
            ReserveProduct.AFRR, HOUR_START, END
        )


def test_capacity_rejects_total_changing_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nordic_balancing.sources.energidataservice.PAGE_SIZE", 1)
    responses = iter([page([CAPACITY], total=2), page([], total=1)])
    with pytest.raises(SourceError, match="changed from 2 to 1"):
        client(lambda _: next(responses)).capacity_market(ReserveProduct.AFRR, HOUR_START, END)


@pytest.fixture
def capacity_validation(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    def forbid_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("capacity validation import must not open a connection")

    monkeypatch.setattr("socket.socket.connect", forbid_network)
    path = Path(__file__).parents[1] / "examples" / "eds_capacity" / "validate.py"
    spec = importlib.util.spec_from_file_location("eds_capacity_validation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def se3_capacity() -> list[ReserveCapacity]:
    row = {**CAPACITY, "PriceArea": "SE3"}
    return client(lambda _: page([row])).capacity_market(
        ReserveProduct.AFRR, HOUR_START, END, zones=[BiddingZone.SE3]
    )


def test_capacity_comparison_counts_hours_once_and_explains_zero_volume_omissions(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity]
) -> None:
    svk = [replace(r, source="svk") for r in se3_capacity if r.direction is Direction.UP]
    report = capacity_validation.compare(se3_capacity, svk)
    assert report.matches == 1
    assert report.shared_hours == 1
    assert report.differences == []
    assert len(report.eds_only) == 1
    assert report.eds_only[0].direction == "DOWN"
    assert report.eds_only[0].volume_mw == 0.0
    assert report.svk_only == []
    assert report.errors == []


@pytest.mark.parametrize("change_price", [True, False], ids=["price_eur_per_mw", "volume_mw"])
def test_capacity_comparison_reports_exact_differences(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity], change_price: bool
) -> None:
    svk = [
        replace(
            r,
            source="svk",
            price_eur_per_mw=0.001 if change_price else r.price_eur_per_mw,
            volume_mw=r.volume_mw if change_price else 0.001,
        )
        for r in se3_capacity
    ]
    report = capacity_validation.compare(se3_capacity, svk)
    assert report.matches == 0
    assert len(report.differences) == 2
    assert report.differences[0].start == HOUR_START.isoformat()
    assert report.differences[0].eds != report.differences[0].svk


def test_capacity_comparison_rejects_incomplete_or_inconsistent_quarters(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity]
) -> None:
    with pytest.raises(SourceError, match="four equal quarters"):
        capacity_validation.compare(se3_capacity[:-1], se3_capacity)
    changed = [replace(se3_capacity[0], volume_mw=1.0), *se3_capacity[1:]]
    with pytest.raises(SourceError, match="four equal quarters"):
        capacity_validation.compare(changed, se3_capacity)


def test_capacity_comparison_does_not_hide_missing_eds_or_nonzero_eds_only(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity]
) -> None:
    down = [r for r in se3_capacity if r.direction is Direction.DOWN]
    eds_only_report = capacity_validation.compare(se3_capacity, down)
    assert eds_only_report.errors
    missing_report = capacity_validation.compare(down, se3_capacity)
    assert len(missing_report.svk_only) == 1
    assert missing_report.errors


def test_capacity_comparison_requires_live_flag_before_fetch(
    capacity_validation: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the script must not fetch without --live")

    monkeypatch.setattr(EnergiDataServiceClient, "capacity_market", forbidden)
    with pytest.raises(SystemExit) as caught:
        capacity_validation.main([])
    assert caught.value.code == 2


def test_capacity_comparison_matches_both_directions_without_counting_quarters(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity]
) -> None:
    report = capacity_validation.compare(se3_capacity, se3_capacity)
    assert report.matches == 2
    assert report.shared_hours == 1
    assert not (report.differences or report.eds_only or report.svk_only or report.errors)


def test_capacity_comparison_refuses_to_pass_without_shared_hours(
    capacity_validation: ModuleType,
) -> None:
    report = capacity_validation.compare([], [])
    assert report.matches == 0
    assert report.shared_hours == 0
    assert report.errors == ["no shared hour/directions; exact equality is unverified"]
