import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

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

Handler = Callable[[httpx.Request], httpx.Response]


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
            httpx.Client(transport=httpx.MockTransport(handler))
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
