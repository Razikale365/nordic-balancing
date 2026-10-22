"""Energi Data Service imbalance prices: parsing, request shape, paging and error handling."""

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fakes import (
    eds_client,
    eds_page,
    http_client,
)

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EnergiDataServiceClient,
    RateLimitError,
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
HOUR_START = START.replace(minute=0)


def imbalance_rows(start: datetime, offset: int, count: int) -> list[dict[str, Any]]:
    return [
        {
            **PUBLISHED,
            "PriceArea": "DK1",
            "TimeUTC": (start + (offset + i) * INTERVAL).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        for i in range(count)
    ]


IMBALANCE: dict[str, Any] = {
    "TimeUTC": "2026-10-09T02:30:00",
    "PriceArea": "DK2",
    "ImbalancePriceEUR": 79.27,
    "ImbalancePriceDKK": 592.46,
    "SpotPriceEUR": 12.5,
    "DominatingDirection": 1,
    "SatisfiedDemand": 34.0,
    "aFRRVWAUpEUR": 95.1,
    "aFRRVWADownEUR": -3.53,
    "mFRRMarginalPriceUpEUR": 88.4,
    "mFRRMarginalPriceDownEUR": 40.14,
}


# --- Live checks against the real API ----------------------------------------------------------


@pytest.mark.live
def test_live_one_hour_of_dk_imbalance_prices() -> None:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    with EnergiDataServiceClient() as eds:
        prices = eds.imbalance_prices(start, start + timedelta(hours=1))

    assert len(prices) == 8  # 4 intervals x DK1, DK2
    assert prices[0].start == start
    assert {p.zone for p in prices} == {BiddingZone.DK1, BiddingZone.DK2}
    assert all(p.imbalance_price_eur is not None for p in prices)


@pytest.mark.live
def test_live_sixty_days_span_more_than_one_page() -> None:
    # 60 days x 96 quarters x 2 zones = 11,520 rows: more than one 10,000-row page.
    start = datetime(2025, 4, 1, tzinfo=UTC)
    with EnergiDataServiceClient() as eds:
        prices = eds.imbalance_prices(start, start + timedelta(days=60))

    assert len(prices) == 60 * 96 * 2
    assert prices[-1].start == start + timedelta(days=60) - INTERVAL


# --- Retries, transport errors and API key handling --------------------------------------------


def test_retries_rate_limit_honouring_retry_after() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429),
            eds_page([PUBLISHED]),
        ]
    )
    sleeps: list[float] = []

    prices = eds_client(lambda _: next(responses), sleeps).imbalance_prices(START, END)

    assert sleeps == [7.0, 10.0]
    assert len(prices) == 1


def test_gives_up_after_max_retries() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitError, match="retry after 3 s"):
        eds_client(handler, sleeps).imbalance_prices(START, END)
    assert sleeps == [3.0, 3.0, 3.0]


def test_http_error_is_a_source_error() -> None:
    with pytest.raises(SourceError, match="HTTP 400"):
        eds_client(lambda _: httpx.Response(400, text="bad filter")).imbalance_prices(START, END)


def test_transport_error_becomes_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    with pytest.raises(SourceError, match="request failed"):
        EnergiDataServiceClient(http_client(handler), sleep=lambda _: None).imbalance_prices(
            START, END
        )


@pytest.mark.parametrize("retries", [-1, True, 1.5, "3"])
def test_rejects_bad_retry_budget(retries: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        EnergiDataServiceClient(max_retries=retries)


@pytest.mark.parametrize("status", [502, 504])
def test_retries_transient_gateway_errors(status: int) -> None:
    responses = iter([httpx.Response(status), eds_page([PUBLISHED])])
    sleeps: list[float] = []
    prices = eds_client(lambda _: next(responses), sleeps).imbalance_prices(START, END)
    assert len(prices) == 1
    assert sleeps == [10.0]


@pytest.mark.parametrize("status", [502, 504])
def test_exhausted_gateway_errors_are_source_errors_not_rate_limits(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    with pytest.raises(SourceError, match=f"HTTP {status}"):
        eds_client(handler).imbalance_prices(START, END)
    assert len(calls) == 4


def test_retries_transport_errors_then_gives_up_as_source_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectTimeout("timed out", request=request)

    sleeps: list[float] = []
    with pytest.raises(SourceError, match="request failed"):
        eds_client(handler, sleeps).imbalance_prices(START, END)
    assert len(calls) == 4
    assert sleeps == [10.0] * 3


def test_transport_error_then_success() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadError("reset", request=request)
        return eds_page([PUBLISHED])

    assert len(eds_client(handler).imbalance_prices(START, END)) == 1


@pytest.mark.parametrize("header", ["inf", "nan", "-inf", "1e400"])
def test_non_finite_retry_after_uses_the_default_delay(header: str) -> None:
    responses = iter([httpx.Response(429, headers={"Retry-After": header}), eds_page([PUBLISHED])])
    sleeps: list[float] = []
    eds_client(lambda _: next(responses), sleeps).imbalance_prices(START, END)
    assert sleeps == [10.0]


def test_retry_after_beyond_the_cap_raises_without_sleeping() -> None:
    sleeps: list[float] = []
    with pytest.raises(RateLimitError) as caught:
        eds_client(
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
        eds_client(handler, sleeps).imbalance_prices(START, END)
    assert len(calls) == 1
    assert sleeps == []


def test_eds_accepts_zero_retry_budget_and_fails_at_once() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"})

    with pytest.raises(RateLimitError):
        EnergiDataServiceClient(
            http_client(handler),
            max_retries=0,
            sleep=lambda _: pytest.fail("no retries means no sleeps"),
        ).imbalance_prices(START, END)
    assert len(calls) == 1


# --- Paging ------------------------------------------------------------------------------------


def test_pages_until_a_short_page_even_when_total_equals_the_page_size() -> None:
    # The API reports `total` as the row count of that page, so a full page always
    # says total == PAGE_SIZE even while more rows remain.
    offsets: list[str] = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row_count = 2 * 10_000 + 7

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(request.url.params["offset"])
        return eds_page(imbalance_rows(start, offset, min(10_000, row_count - offset)))

    prices = eds_client(handler).imbalance_prices(
        start, start + timedelta(days=210), [BiddingZone.DK1]
    )

    assert offsets == ["0", "10000", "20000"]
    assert len(prices) == row_count


def test_an_exact_multiple_of_the_page_size_ends_on_an_empty_page() -> None:
    offsets: list[str] = []
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(request.url.params["offset"])
        return eds_page(imbalance_rows(start, offset, min(10_000, 20_000 - offset)))

    prices = eds_client(handler).imbalance_prices(
        start, start + timedelta(days=210), [BiddingZone.DK1]
    )

    assert offsets == ["0", "10000", "20000"]
    assert len(prices) == 20_000


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
        eds_client(lambda _: httpx.Response(200, json=body)).imbalance_prices(START, END)


@pytest.mark.parametrize(
    ("records", "total"),
    [([PUBLISHED], 5), ([PUBLISHED, NOT_YET_PUBLISHED], 1), ([], 1)],
)
def test_rejects_total_not_matching_the_page_row_count(
    records: list[dict[str, Any]], total: int
) -> None:
    message = f"total {total} does not match {len(records)} records"
    with pytest.raises(SourceError, match=message):
        eds_client(lambda _: eds_page(records, total=total)).imbalance_prices(START, END)


# --- Arguments and client lifecycle ------------------------------------------------------------


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
    c = eds_client(lambda _: eds_page([]))
    with pytest.raises(ValueError, match=message):
        if zones is None:
            c.imbalance_prices(start, end)
        else:
            c.imbalance_prices(start, end, zones)


def test_closes_only_its_own_http_client() -> None:
    http = http_client(lambda _: eds_page([]))
    with EnergiDataServiceClient(http):
        pass
    assert not http.is_closed


@pytest.mark.parametrize("zone", [BiddingZone.FI, BiddingZone.NO1, BiddingZone.SE3])
def test_rejects_non_danish_zone_arguments(zone: BiddingZone) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("non-Danish zones must not send an HTTP request")

    with pytest.raises(ValueError, match="only DK1 and DK2"):
        eds_client(handler).imbalance_prices(START, END, [zone])


# --- Malformed records and responses -----------------------------------------------------------


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
        eds_client(lambda _: eds_page([record])).imbalance_prices(START, END)


@pytest.mark.parametrize("record", [1, None, "x", []])
def test_rejects_record_that_is_not_an_object(record: object) -> None:
    body = {"total": 1, "records": [record]}

    with pytest.raises(SourceError, match="not a JSON object"):
        eds_client(lambda _: httpx.Response(200, json=body)).imbalance_prices(START, END)


def test_rejects_body_that_is_not_an_object() -> None:
    with pytest.raises(SourceError, match="is not a JSON object"):
        eds_client(lambda _: httpx.Response(200, json=[1, 2, 3])).imbalance_prices(START, END)


@pytest.mark.parametrize("column", list(PUBLISHED))
def test_rejects_missing_columns(column: str) -> None:
    record = {key: value for key, value in PUBLISHED.items() if key != column}

    with pytest.raises(SourceError, match=f"missing imbalance columns.*{column}"):
        eds_client(lambda _: eds_page([record])).imbalance_prices(START, END)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_numbers(value: str) -> None:
    record = {**PUBLISHED, "ImbalancePriceEUR": json.loads(value)}
    body = json.dumps({"total": 1, "records": [record]})
    with pytest.raises(SourceError, match="not finite"):
        eds_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(START, END)


def test_rejects_duplicate_zone_and_interval() -> None:
    duplicate = {**PUBLISHED, "ImbalancePriceEUR": 99.0}
    with pytest.raises(SourceError, match="duplicate"):
        eds_client(lambda _: eds_page([PUBLISHED, duplicate])).imbalance_prices(START, END)


def test_rejects_unrequested_zone() -> None:
    with pytest.raises(SourceError, match="unrequested PriceArea"):
        eds_client(lambda _: eds_page([PUBLISHED])).imbalance_prices(START, END, [BiddingZone.DK1])


@pytest.mark.parametrize("time_utc", ["2026-10-09T02:00:00", "2026-10-09T03:00:00"])
def test_rejects_records_outside_the_query_window(time_utc: str) -> None:
    record = {**PUBLISHED, "TimeUTC": time_utc}
    with pytest.raises(SourceError, match="outside requested window"):
        eds_client(lambda _: eds_page([record])).imbalance_prices(START, END)


def test_rejects_hourly_only_rows_in_the_15_minute_dataset() -> None:
    # ImbalancePrice publishes every quarter; a zone returning only on-the-hour
    # rows across more than one hour is an hourly cadence, not a gap, and must
    # not be labelled resolution=INTERVAL.
    rows = [
        {**PUBLISHED, "TimeUTC": "2026-10-09T02:00:00"},
        {**PUBLISHED, "TimeUTC": "2026-10-09T03:00:00"},
    ]
    with pytest.raises(SourceError, match="only on the hour"):
        eds_client(lambda _: eds_page(rows)).imbalance_prices(
            HOUR_START, HOUR_START + timedelta(hours=2)
        )


@pytest.mark.parametrize("value", [0.6, -0.4, 1.9, -1.5])
def test_rejects_fractional_dominating_direction(value: float) -> None:
    # int() would silently truncate these to a valid direction.
    record = {**PUBLISHED, "DominatingDirection": value}
    with pytest.raises(SourceError, match="unknown DominatingDirection"):
        eds_client(lambda _: eds_page([record])).imbalance_prices(START, END)


@pytest.mark.parametrize(
    "time_utc",
    ["2026-10-09T02:30:00.5", "2026-10-09T02:30:00.000001", "2026-10-09T02:30:30"],
)
def test_rejects_sub_minute_offsets_from_the_quarter_hour_grid(time_utc: str) -> None:
    record = {**PUBLISHED, "TimeUTC": time_utc}
    with pytest.raises(SourceError, match="15-minute boundary"):
        eds_client(lambda _: eds_page([record])).imbalance_prices(START, END)


@pytest.mark.parametrize(
    "time_utc",
    [
        "2026-10-09",
        "20261009",
        "T",
        "2026-13-99T99:99",
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
        eds_client(lambda _: eds_page([record])).imbalance_prices(start, start + timedelta(hours=3))
    valid_records = [
        {**PUBLISHED, "TimeUTC": "2026-10-09T02:30:00Z", "PriceArea": "DK1"},
        {**PUBLISHED, "TimeUTC": "2026-10-09T02:30:00+00:00", "PriceArea": "DK2"},
    ]
    prices = eds_client(lambda _: eds_page(valid_records)).imbalance_prices(START, END)
    assert [p.start for p in prices] == [START, START]


def test_imbalance_rejects_empty_window() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        eds_client(lambda _: eds_page([])).imbalance_prices(START, START)


# --- Query windows and timestamps --------------------------------------------------------------


def test_sends_utc_window_and_zone_filter() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return eds_page([])

    copenhagen = timezone(timedelta(hours=2))
    eds_client(handler).imbalance_prices(
        START.astimezone(copenhagen), END.astimezone(copenhagen), [BiddingZone.DK1]
    )

    params = seen[0].url.params
    assert seen[0].url.path == "/dataset/ImbalancePrice"
    assert params["start"] == "2026-10-09T02:30"
    assert params["end"] == "2026-10-09T03:00"
    assert params["timezone"] == "UTC"
    assert json.loads(params["filter"]) == {"PriceArea": ["DK1"]}


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
        return eds_page([before_start, last_interval])

    prices = eds_client(handler).imbalance_prices(
        START + timedelta(seconds=30), END - timedelta(seconds=30)
    )

    params = seen[0].url.params
    assert params["start"] == "2026-10-09T02:30"
    assert params["end"] == "2026-10-09T03:00"
    assert [p.start for p in prices] == [START + INTERVAL]


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
    prices = eds_client(lambda _: eds_page([record])).imbalance_prices(window_start, END)
    assert [p.start for p in prices] == [expected_start]


# --- Parsing and request shape -----------------------------------------------------------------


def test_parses_published_and_unpublished_intervals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return eds_page([NOT_YET_PUBLISHED, PUBLISHED])

    prices = eds_client(handler).imbalance_prices(START, END)

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


def test_explicit_null_columns_stay_none() -> None:
    record = {**PUBLISHED, "ImbalancePriceEUR": None, "SpotPriceEUR": None}

    prices = eds_client(lambda _: eds_page([record])).imbalance_prices(START, END)

    assert prices[0].imbalance_price_eur is None
    assert prices[0].spot_price_eur is None


def test_accepts_on_hour_rows_when_a_quarter_row_is_present() -> None:
    rows = [
        {**PUBLISHED, "TimeUTC": "2026-10-09T02:00:00"},
        {**PUBLISHED, "TimeUTC": "2026-10-09T02:15:00"},
        {**PUBLISHED, "TimeUTC": "2026-10-09T03:00:00"},
    ]
    prices = eds_client(lambda _: eds_page(rows)).imbalance_prices(
        HOUR_START, HOUR_START + timedelta(hours=2)
    )
    assert len(prices) == 3


def test_preserves_resolution_raw_and_renamed_mfrr_fields() -> None:
    price = eds_client(lambda _: eds_page([PUBLISHED])).imbalance_prices(START, END)[0]
    assert price.resolution == timedelta(minutes=15)
    assert price.raw == PUBLISHED
    assert price.mfrr_up_price_eur == 79.27
    assert price.mfrr_down_price_eur == 40.14
    with pytest.raises(TypeError):
        price.raw["PriceArea"] = "DK1"  # type: ignore[index]


@pytest.mark.parametrize(
    ("value", "expected"), [(-1, Direction.DOWN), (0, Direction.NONE), (1.0, Direction.UP)]
)
def test_accepts_exact_dominating_directions(value: float, expected: Direction) -> None:
    record = {**PUBLISHED, "DominatingDirection": value}
    price = eds_client(lambda _: eds_page([record])).imbalance_prices(START, END)[0]
    assert price.dominating_direction is expected


def test_null_dominating_direction_is_none() -> None:
    record = {**PUBLISHED, "DominatingDirection": None}
    price = eds_client(lambda _: eds_page([record])).imbalance_prices(START, END)[0]
    assert price.dominating_direction is None
    assert price.imbalance_price_eur == PUBLISHED["ImbalancePriceEUR"]


def test_imbalance_maps_every_field_to_its_column() -> None:
    price = eds_client(lambda _: eds_page([IMBALANCE])).imbalance_prices(START, END)[0]
    assert price.imbalance_price_eur == 79.27
    assert price.imbalance_price_dkk == 592.46
    assert price.spot_price_eur == 12.5
    assert price.dominating_direction is Direction.UP
    assert price.satisfied_demand_mw == 34.0
    assert price.afrr_up_vwa_eur == 95.1
    assert price.afrr_down_vwa_eur == -3.53
    assert price.mfrr_up_price_eur == 88.4
    assert price.mfrr_down_price_eur == 40.14


def test_imbalance_request_sorts_dedupes_zones_and_names_columns() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return eds_page([])

    eds_client(handler).imbalance_prices(
        START, END, (z for z in (BiddingZone.DK2, BiddingZone.DK1, BiddingZone.DK2))
    )
    params = seen[0].url.params
    assert json.loads(params["filter"]) == {"PriceArea": ["DK1", "DK2"]}
    assert params["sort"] == "TimeUTC asc,PriceArea asc"
    assert params["columns"].split(",") == [
        "TimeUTC",
        "PriceArea",
        "ImbalancePriceEUR",
        "ImbalancePriceDKK",
        "SpotPriceEUR",
        "DominatingDirection",
        "SatisfiedDemand",
        "aFRRVWAUpEUR",
        "aFRRVWADownEUR",
        "mFRRMarginalPriceUpEUR",
        "mFRRMarginalPriceDownEUR",
    ]


def test_imbalance_prices_reject_a_plain_zone_string() -> None:
    with pytest.raises(ValueError, match="zones must contain only"):
        eds_client(lambda _: eds_page([])).imbalance_prices(
            datetime(2026, 1, 15, tzinfo=UTC),
            datetime(2026, 1, 15, 1, tzinfo=UTC),
            "DK1",  # type: ignore[arg-type]
        )
