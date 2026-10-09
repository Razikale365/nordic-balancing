from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    ESettClient,
    RateLimitError,
    SourceError,
)
from nordic_balancing.sources import ESettClient as SourceESettClient

PUBLISHED: dict[str, Any] = {
    "timestamp": "2026-01-15T01:00:00",
    "timestampUTC": "2026-01-15T00:00:00Z",
    "mba": "SE3",
    "imblSalesPrice": 21.0,
    "imblPurchasePrice": 21.0,
    "upRegPrice": 76.32,
    "downRegPrice": 21.0,
    "mainDirRegPowerPerMBA": -1.0,
    "imblSpotDifferencePrice": -55.32,
    "upRegPriceFrrA": None,
}
START = datetime(2026, 1, 15, tzinfo=UTC)
END = START + timedelta(hours=1)
Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler, sleeps: list[float] | None = None) -> ESettClient:
    recorded = sleeps if sleeps is not None else []
    return ESettClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=recorded.append,
    )


def test_parses_published_and_null_prices_and_sorts() -> None:
    pending = {
        **PUBLISHED,
        "timestampUTC": "2026-01-15T00:15:00Z",
        "mba": "FI",
        "imblSalesPrice": None,
        "imblPurchasePrice": None,
        "upRegPrice": None,
        "downRegPrice": None,
        "mainDirRegPowerPerMBA": None,
    }
    other_zone = {**PUBLISHED, "mba": "FI", "mainDirRegPowerPerMBA": 1.0}
    prices = client(
        lambda _: httpx.Response(200, json=[pending, PUBLISHED, other_zone])
    ).imbalance_prices(START, END)

    assert [(p.start, p.zone) for p in prices] == [
        (START, BiddingZone.FI),
        (START, BiddingZone.SE3),
        (START + INTERVAL, BiddingZone.FI),
    ]
    assert prices[0].dominating_direction is Direction.UP
    published, unpublished = prices[1:]
    assert published.imbalance_price_eur == 21.0
    assert published.mfrr_up_price_eur == 76.32
    assert published.mfrr_down_price_eur == 21.0
    assert published.dominating_direction is Direction.DOWN
    assert published.source == "esett"
    assert published.resolution == INTERVAL
    assert published.end == START + INTERVAL
    assert published.imbalance_price_dkk is None
    assert published.spot_price_eur is None
    assert published.satisfied_demand_mw is None
    assert published.afrr_up_vwa_eur is None
    assert published.afrr_down_vwa_eur is None
    assert unpublished.imbalance_price_eur is None
    assert unpublished.mfrr_up_price_eur is None
    assert unpublished.mfrr_down_price_eur is None
    assert unpublished.dominating_direction is None


def test_preserves_read_only_raw_without_affecting_equality_or_repr() -> None:
    price = client(lambda _: httpx.Response(200, json=[PUBLISHED])).imbalance_prices(START, END)[0]
    assert price.raw == PUBLISHED
    assert price.raw["timestamp"] == "2026-01-15T01:00:00"
    with pytest.raises(TypeError):
        price.raw["mba"] = "FI"  # type: ignore[index]
    mutable = {"extra": 1}
    copied = replace(price, raw=mutable)
    mutable["extra"] = 2
    assert copied.raw["extra"] == 1
    assert copied == price
    assert "raw=" not in repr(price)


def test_sends_utc_window_repeated_codes_and_custom_base_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    local = timezone(timedelta(hours=2))
    with ESettClient(http, base_url="https://example.test/") as esett:
        esett.imbalance_prices(
            START.astimezone(local),
            END.astimezone(local),
            [BiddingZone.SE3, BiddingZone.FI, BiddingZone.SE3],
        )
    assert seen[0].url.host == "example.test"
    assert seen[0].url.path == "/EXP14/Prices"
    params = seen[0].url.params
    assert params["start"] == "2026-01-15T00:00:00.000Z"
    assert params["end"] == "2026-01-15T01:00:00.000Z"
    assert params.get_list("mba") == ["10YFI_1________U", "10Y1001A1001A46L"]
    assert "Authorization" not in seen[0].headers


def test_defaults_to_all_twelve_known_eic_codes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client(handler).imbalance_prices(START, END)
    assert len(BiddingZone) == 12
    assert seen[0].url.params.get_list("mba") == [
        "10YDK-1--------W",
        "10YDK-2--------M",
        "10YFI_1________U",
        "10YNO_1________2",
        "10YNO_2________T",
        "10YNO_3________J",
        "10YNO_4________9",
        "10Y1001A1001A48H",
        "10Y1001A1001A44P",
        "10Y1001A1001A45N",
        "10Y1001A1001A46L",
        "10Y1001A1001A47J",
    ]


def test_chunks_long_windows_without_gaps_or_duplicate_intervals() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {**PUBLISHED, "timestampUTC": request.url.params["start"]},
            ],
        )

    end = START + timedelta(days=65)
    prices = client(handler).imbalance_prices(START, end, [BiddingZone.SE3])
    windows = [
        (datetime.fromisoformat(r.url.params["start"]), datetime.fromisoformat(r.url.params["end"]))
        for r in seen
    ]
    assert windows == [
        (START, START + timedelta(days=31)),
        (START + timedelta(days=31), START + timedelta(days=62)),
        (START + timedelta(days=62), end),
    ]
    assert all(r.url.params.get_list("mba") == ["10Y1001A1001A46L"] for r in seen)
    assert [p.start for p in prices] == [w[0] for w in windows]
    assert all(p.resolution == INTERVAL for p in prices)


def test_expands_hourly_records_and_filters_a_non_hour_start() -> None:
    seen: list[httpx.Request] = []
    hour = datetime(2023, 5, 21, 20, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {**PUBLISHED, "timestampUTC": "2023-05-21T20:00:00Z"},
                {**PUBLISHED, "timestampUTC": "2023-05-21T21:00:00Z"},
            ],
        )

    prices = client(handler).imbalance_prices(
        hour + timedelta(minutes=20), hour + timedelta(hours=1, minutes=40), [BiddingZone.SE3]
    )
    assert seen[0].url.params["start"] == "2023-05-21T20:00:00.000Z"
    assert [p.start for p in prices] == [hour + i * INTERVAL for i in range(2, 7)]
    assert all(p.resolution == timedelta(hours=1) for p in prices)
    assert all(p.imbalance_price_eur == 21.0 for p in prices)
    assert prices[1].raw["timestampUTC"] == "2023-05-21T20:00:00Z"


def test_resolution_transition_preserves_source_publication_intervals() -> None:
    start = datetime(2023, 5, 21, 20, tzinfo=UTC)
    records = [
        {**PUBLISHED, "timestampUTC": (start + timedelta(hours=i)).isoformat()} for i in range(2)
    ] + [
        {**PUBLISHED, "timestampUTC": (start + timedelta(hours=2) + i * INTERVAL).isoformat()}
        for i in range(4)
    ]
    prices = client(lambda _: httpx.Response(200, json=records)).imbalance_prices(
        start, start + timedelta(hours=3), [BiddingZone.SE3]
    )
    assert [p.start for p in prices] == [start + i * INTERVAL for i in range(12)]
    assert [p.resolution for p in prices] == [timedelta(hours=1)] * 8 + [INTERVAL] * 4


def test_filters_subinterval_start_and_preserves_exclusive_end_precision() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {**PUBLISHED, "timestampUTC": (START + INTERVAL).isoformat()},
            ],
        )

    prices = client(handler).imbalance_prices(
        START + timedelta(microseconds=1), START + INTERVAL + timedelta(microseconds=1)
    )
    assert [p.start for p in prices] == [START + INTERVAL]
    assert seen[0].url.params["start"] == "2026-01-15T00:00:00.000Z"
    assert seen[0].url.params["end"] == "2026-01-15T00:15:00.001Z"


@pytest.mark.parametrize(
    "override",
    [
        {"imblPurchasePrice": None},
        {"imblSalesPrice": None},
        {"imblSalesPrice": None, "imblPurchasePrice": None},
    ],
)
def test_allows_one_or_both_single_prices_to_be_missing(override: dict[str, Any]) -> None:
    record = {**PUBLISHED, **override}
    price = client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)[0]
    # Single-price model: whichever column is published is the price; both null -> None.
    expected = record["imblSalesPrice"]
    if expected is None:
        expected = record["imblPurchasePrice"]
    assert price.imbalance_price_eur == expected


def test_purchase_only_price_is_kept_not_dropped() -> None:
    record = {**PUBLISHED, "imblSalesPrice": None, "imblPurchasePrice": 33.5}
    price = client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)[0]
    assert price.imbalance_price_eur == 33.5
    assert price.raw["imblSalesPrice"] is None


def test_rejects_sales_purchase_disagreement() -> None:
    record = {**PUBLISHED, "imblPurchasePrice": 22}
    with pytest.raises(SourceError, match="esett: single-price model violated"):
        client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)


@pytest.mark.parametrize("price", [21.0, 22.0])
def test_rejects_duplicate_zone_and_interval(price: float) -> None:
    duplicate = {**PUBLISHED, "imblSalesPrice": price, "imblPurchasePrice": price}
    with pytest.raises(SourceError, match="duplicate"):
        client(lambda _: httpx.Response(200, json=[PUBLISHED, duplicate])).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"timestampUTC": None}, "timestampUTC is missing"),
        ({"timestampUTC": "invalid"}, "unparseable timestampUTC"),
        ({"timestampUTC": "2026-01-15T00:00:00"}, "must be UTC"),
        ({"timestampUTC": "2026-01-15T01:00:00+01:00"}, "must be UTC"),
        ({"timestampUTC": "2026-01-15T00:05:00Z"}, "15-minute boundary"),
        ({"timestampUTC": "2026-01-15T00:00:00.000001Z"}, "15-minute boundary"),
        ({"timestampUTC": "2026-01-15T00:00:01Z"}, "15-minute boundary"),
        ({"timestampUTC": "2023-05-21T20:15:00Z"}, "not on the hour"),
        ({"mba": None}, "unknown mba"),
        ({"mba": "UNKNOWN"}, "unknown mba"),
        ({"mba": "10Y1001A1001A46L"}, "unknown mba"),
        ({"imblSalesPrice": "21"}, "not a number"),
        ({"imblPurchasePrice": True}, "not a number"),
        ({"upRegPrice": False}, "not a number"),
        ({"downRegPrice": []}, "not a number"),
        ({"mainDirRegPowerPerMBA": 2.0}, "unknown mainDirRegPowerPerMBA"),
        ({"mainDirRegPowerPerMBA": 0.5}, "unknown mainDirRegPowerPerMBA"),
    ],
)
def test_rejects_malformed_records(override: dict[str, Any], message: str) -> None:
    record = {**PUBLISHED, **override}
    with pytest.raises(SourceError, match=message):
        client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_prices(value: str) -> None:
    body = '[{"timestampUTC":"2026-01-15T00:00:00Z","mba":"SE3","imblSalesPrice":' + value + "}]"
    with pytest.raises(SourceError, match="not finite"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(START, END)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "not a JSON array"),
        ([None], "not a JSON object"),
        (["bad"], "not a JSON object"),
    ],
)
def test_rejects_malformed_response_shapes(body: object, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        client(lambda _: httpx.Response(200, json=body)).imbalance_prices(START, END)


def test_rejects_unrequested_zone_and_out_of_window_record() -> None:
    with pytest.raises(SourceError, match="unrequested mba"):
        client(lambda _: httpx.Response(200, json=[PUBLISHED])).imbalance_prices(
            START, END, [BiddingZone.FI]
        )
    record = {**PUBLISHED, "timestampUTC": END.isoformat()}
    with pytest.raises(SourceError, match="outside requested chunk"):
        client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(400, text="bad request"), "esett: HTTP 400"),
        (httpx.Response(200, text="not json"), "esett: response .* is not JSON"),
    ],
)
def test_source_errors_name_esett(response: httpx.Response, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        client(lambda _: response).imbalance_prices(START, END)


def test_shared_retry_path_honours_retry_after_and_fallback() -> None:
    responses = iter(
        [
            httpx.Response(503, headers={"Retry-After": "2.5"}),
            httpx.Response(429, headers={"Retry-After": "invalid"}),
            httpx.Response(200, json=[PUBLISHED]),
        ]
    )
    sleeps: list[float] = []
    prices = client(lambda _: next(responses), sleeps).imbalance_prices(START, END)
    assert sleeps == [2.5, 10.0]
    assert len(prices) == 1


@pytest.mark.parametrize("status", [429, 503])
def test_retry_budget_is_bounded_and_reports_source(status: int) -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitError) as caught:
        client(handler, sleeps).imbalance_prices(START, END)
    assert caught.value.source == "esett"
    assert caught.value.retry_after == 3.0
    assert len(calls) == 4
    assert sleeps == [3.0] * 3


def test_zero_retries_does_not_sleep() -> None:
    sleeps: list[float] = []
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "7"}))
    )
    with pytest.raises(RateLimitError):
        ESettClient(http, max_retries=0, sleep=sleeps.append).imbalance_prices(START, END)
    assert sleeps == []


@pytest.mark.parametrize(
    ("start", "end", "zones", "message"),
    [
        (START.replace(tzinfo=None), END, None, "timezone-aware"),
        (START, END.replace(tzinfo=None), None, "timezone-aware"),
        (END, START, None, "end must be after start"),
        (START, START, None, "end must be after start"),
        (START, END, [], "zones must not be empty"),
    ],
)
def test_rejects_bad_arguments_without_request(
    start: datetime, end: datetime, zones: list[BiddingZone] | None, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("bad arguments must not send an HTTP request")

    c = client(handler)
    with pytest.raises(ValueError, match=message):
        if zones is None:
            c.imbalance_prices(start, end)
        else:
            c.imbalance_prices(start, end, zones)


@pytest.mark.parametrize("zone", ["SE3", "unknown", 1, None])
def test_rejects_non_enum_zones_without_request(zone: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("unknown zones must not send an HTTP request")

    with pytest.raises(ValueError, match="BiddingZone"):
        client(handler).imbalance_prices(START, END, [zone])


@pytest.mark.parametrize("retries", [-1, True, 1.5])
def test_rejects_bad_retry_budget(retries: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        ESettClient(max_retries=retries)


def test_context_manager_closes_only_owned_client_and_exports() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])))
    with ESettClient(http):
        pass
    assert not http.is_closed
    with ESettClient() as owned:
        assert not owned._http.is_closed
    assert owned._http.is_closed
    owned.close()
    assert SourceESettClient is ESettClient


@pytest.mark.live
def test_live_one_hour_of_se3_and_fi() -> None:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    with ESettClient() as esett:
        prices = esett.imbalance_prices(
            start, start + timedelta(hours=1), [BiddingZone.SE3, BiddingZone.FI]
        )
    assert len(prices) == 8
    assert [(p.start, p.zone) for p in prices] == [
        (start + i * INTERVAL, zone) for i in range(4) for zone in (BiddingZone.FI, BiddingZone.SE3)
    ]
    assert all(p.resolution == INTERVAL for p in prices)
    assert all(p.imbalance_price_eur is not None for p in prices)


@pytest.mark.live
def test_live_hourly_to_quarter_hour_transition() -> None:
    start = datetime(2023, 5, 21, 20, tzinfo=UTC)
    with ESettClient() as esett:
        prices = esett.imbalance_prices(start, start + timedelta(hours=3), [BiddingZone.SE3])
    assert len(prices) == 12
    assert [p.start for p in prices] == [start + i * INTERVAL for i in range(12)]
    assert [p.resolution for p in prices] == [timedelta(hours=1)] * 8 + [INTERVAL] * 4
    assert all(p.zone is BiddingZone.SE3 for p in prices)
    assert all(p.imbalance_price_eur is not None for p in prices)
    assert len({p.imbalance_price_eur for p in prices[:4]}) == 1
    assert len({p.imbalance_price_eur for p in prices[4:8]}) == 1


def test_transport_error_becomes_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    with pytest.raises(SourceError, match="request failed"):
        ESettClient(httpx.Client(transport=httpx.MockTransport(handler))).imbalance_prices(
            START, END
        )
