"""eSett imbalance prices: parsing, resolution switch, placeholders for unsettled Danish rows."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fakes import (
    esett_client,
    http_client,
)

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


# Verbatim eSett rows captured live 2026-10-18: while DK intervals are
# unsettled every price column arrives as exactly 0.0, not null.
DK_PLACEHOLDER: dict[str, Any] = {
    "timestamp": "2026-10-18T22:30:00",
    "timestampUTC": "2026-10-18T20:30:00Z",
    "mba": "DK2",
    "imblSalesPrice": 0.0,
    "imblPurchasePrice": 0.0,
    "upRegPrice": 0.0,
    "downRegPrice": 0.0,
    "mainDirRegPowerPerMBA": 0.0,
    "imblSpotDifferencePrice": -170.92,
    "upRegPriceFrrA": 0.0,
    "downRegPriceFrrA": 0.0,
    "valueOfAvoidedActivation": None,
    "incentivisingComponent": None,
}


# --- Live checks against the real API ----------------------------------------------------------


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


@pytest.mark.live
def test_live_history_starts_2021_10_31_23z() -> None:
    first = datetime(2021, 10, 31, 23, tzinfo=UTC)
    with ESettClient() as esett:
        prices = esett.imbalance_prices(
            first - timedelta(hours=3), first + timedelta(hours=2), [BiddingZone.SE3]
        )
    assert prices
    assert min(p.start for p in prices) == first


# --- Retries, transport errors and API key handling --------------------------------------------


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(400, text="bad request"), "esett: HTTP 400"),
        (httpx.Response(200, text="not json"), "esett: response .* is not JSON"),
    ],
)
def test_source_errors_name_esett(response: httpx.Response, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        esett_client(lambda _: response).imbalance_prices(START, END)


def test_shared_retry_path_honours_retry_after_and_fallback() -> None:
    responses = iter(
        [
            httpx.Response(503, headers={"Retry-After": "2.5"}),
            httpx.Response(429, headers={"Retry-After": "invalid"}),
            httpx.Response(200, json=[PUBLISHED]),
        ]
    )
    sleeps: list[float] = []
    prices = esett_client(lambda _: next(responses), sleeps).imbalance_prices(START, END)
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
        esett_client(handler, sleeps).imbalance_prices(START, END)
    assert caught.value.source == "esett"
    assert caught.value.retry_after == 3.0
    assert len(calls) == 4
    assert sleeps == [3.0] * 3


def test_zero_retries_does_not_sleep() -> None:
    sleeps: list[float] = []
    http = http_client(lambda _: httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(RateLimitError):
        ESettClient(http, max_retries=0, sleep=sleeps.append).imbalance_prices(START, END)
    assert sleeps == []


@pytest.mark.parametrize("retries", [-1, True, 1.5])
def test_rejects_bad_retry_budget(retries: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        ESettClient(max_retries=retries)


def test_transport_error_becomes_source_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    with pytest.raises(SourceError, match="request failed"):
        ESettClient(http_client(handler), sleep=lambda _: None).imbalance_prices(START, END)


# --- Arguments and client lifecycle ------------------------------------------------------------


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

    c = esett_client(handler)
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
        esett_client(handler).imbalance_prices(START, END, [zone])


def test_context_manager_closes_only_owned_client_and_exports() -> None:
    http = http_client(lambda _: httpx.Response(200, json=[]))
    with ESettClient(http):
        pass
    assert not http.is_closed
    with ESettClient() as owned:
        assert not owned._http.is_closed
    assert owned._http.is_closed
    owned.close()
    assert SourceESettClient is ESettClient


# --- Malformed records and responses -----------------------------------------------------------


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
    prices = esett_client(handler).imbalance_prices(START, end, [BiddingZone.SE3])
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


@pytest.mark.parametrize("offset", [timedelta(seconds=28.5), timedelta(minutes=7, milliseconds=1)])
def test_chunk_boundaries_stay_on_the_quarter_hour_grid(offset: timedelta) -> None:
    # Live eSett leaves out the interval at an unaligned boundary from both chunks.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    esett_client(handler).imbalance_prices(
        START + offset, START + timedelta(days=65), [BiddingZone.SE3]
    )
    inner = [datetime.fromisoformat(r.url.params["end"]) for r in seen[:-1]]
    assert inner == [START + timedelta(days=31), START + timedelta(days=62)]
    assert [datetime.fromisoformat(r.url.params["start"]) for r in seen[1:]] == inner


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
    price = esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)[
        0
    ]
    # Single-price model: whichever column is published is the price; both null -> None.
    expected = record["imblSalesPrice"]
    if expected is None:
        expected = record["imblPurchasePrice"]
    assert price.imbalance_price_eur == expected


def test_rejects_sales_purchase_disagreement() -> None:
    record = {**PUBLISHED, "imblPurchasePrice": 22}
    with pytest.raises(SourceError, match="esett: single-price model violated"):
        esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)


@pytest.mark.parametrize("price", [21.0, 22.0])
def test_rejects_duplicate_zone_and_interval(price: float) -> None:
    duplicate = {**PUBLISHED, "imblSalesPrice": price, "imblPurchasePrice": price}
    with pytest.raises(SourceError, match="duplicate"):
        esett_client(lambda _: httpx.Response(200, json=[PUBLISHED, duplicate])).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"timestampUTC": None}, "timestampUTC is missing"),
        ({"timestampUTC": "invalid"}, "unparseable timestampUTC"),
        ({"timestampUTC": "2026-01-15_00:00:00Z"}, "unparseable timestampUTC"),
        ({"timestampUTC": "2026-01-15x00:00:00Z"}, "unparseable timestampUTC"),
        ({"timestampUTC": "2026-01-15\n00:00:00Z"}, "unparseable timestampUTC"),
        ({"timestampUTC": "2026-01-15500:00:00Z"}, "unparseable timestampUTC"),
        ({"timestampUTC": "2026-01-15T00:00:00"}, "must be UTC"),
        ({"timestampUTC": "2026-01-15T01:00:00+01:00"}, "must be UTC"),
        ({"timestampUTC": "2026-01-15T01:00:00-01:00"}, "must be UTC"),
        ({"timestampUTC": "2026-01-15T00:00:00-00:00"}, "must be UTC"),
        ({"timestampUTC": "2026-01-15T00:00:00+00"}, "must be UTC"),
        ({"timestampUTC": "2026-01-15T00:00:00+0000"}, "must be UTC"),
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
        esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)


@pytest.mark.parametrize(
    "key",
    ["imblSalesPrice", "imblPurchasePrice", "upRegPrice", "downRegPrice", "mainDirRegPowerPerMBA"],
)
def test_rejects_a_record_missing_a_value_key(key: str) -> None:
    # eSett sends every key, null when unpublished; a missing key is a schema change,
    # which must not be reported as an unpublished value.
    record = {name: value for name, value in PUBLISHED.items() if name != key}
    with pytest.raises(SourceError, match=f"missing {key}"):
        esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_prices(value: str) -> None:
    body = (
        '[{"timestampUTC":"2026-01-15T00:00:00Z","mba":"SE3","imblSalesPrice":'
        + value
        + ',"imblPurchasePrice":null,"upRegPrice":null,"downRegPrice":null,'
        '"mainDirRegPowerPerMBA":null}]'
    )
    with pytest.raises(SourceError, match="not finite"):
        esett_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(START, END)


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
        esett_client(lambda _: httpx.Response(200, json=body)).imbalance_prices(START, END)


def test_rejects_unrequested_zone_and_out_of_window_record() -> None:
    with pytest.raises(SourceError, match="unrequested mba"):
        esett_client(lambda _: httpx.Response(200, json=[PUBLISHED])).imbalance_prices(
            START, END, [BiddingZone.FI]
        )
    record = {**PUBLISHED, "timestampUTC": END.isoformat()}
    with pytest.raises(SourceError, match="outside requested chunk"):
        esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)


def test_error_messages_bound_echoed_provider_values() -> None:
    record = {**PUBLISHED, "timestampUTC": "x" * 10_000}
    with pytest.raises(SourceError, match="unparseable timestampUTC") as caught:
        esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)
    message = str(caught.value)
    assert len(message) < 200
    assert "timestampUTC" in message


# --- Unsettled Danish rows ---------------------------------------------------------------------


def test_dk_all_zero_row_is_the_unpublished_placeholder() -> None:
    prices = esett_client(lambda _: httpx.Response(200, json=[DK_PLACEHOLDER])).imbalance_prices(
        datetime(2026, 10, 18, 20, 30, tzinfo=UTC),
        datetime(2026, 10, 18, 20, 45, tzinfo=UTC),
        [BiddingZone.DK2],
    )
    assert len(prices) == 1
    placeholder = prices[0]
    assert placeholder.imbalance_price_eur is None
    assert placeholder.dominating_direction is None
    assert placeholder.mfrr_up_price_eur is None
    assert placeholder.mfrr_down_price_eur is None
    # raw keeps the published zeros.
    assert placeholder.raw["imblSalesPrice"] == 0.0
    assert placeholder.raw["imblSpotDifferencePrice"] == -170.92


def test_dk_settled_row_with_a_single_zero_price_is_real() -> None:
    record = {
        **DK_PLACEHOLDER,
        "downRegPrice": 0.0,
        "imblSalesPrice": 42.0,
        "imblPurchasePrice": 42.0,
        "upRegPrice": 50.0,
    }
    prices = esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(
        datetime(2026, 10, 18, 20, 30, tzinfo=UTC),
        datetime(2026, 10, 18, 20, 45, tzinfo=UTC),
        [BiddingZone.DK2],
    )
    assert prices[0].imbalance_price_eur == 42.0
    assert prices[0].mfrr_down_price_eur == 0.0
    assert prices[0].dominating_direction is Direction.NONE


def test_dk_equal_non_zero_prices_without_a_direction_stay_real() -> None:
    # Only all-zero prices make the placeholder: equal prices alone are a real price.
    record = {
        **DK_PLACEHOLDER,
        "imblSalesPrice": 50.0,
        "imblPurchasePrice": 50.0,
        "upRegPrice": 50.0,
        "downRegPrice": 50.0,
    }
    prices = esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(
        datetime(2026, 10, 18, 20, 30, tzinfo=UTC),
        datetime(2026, 10, 18, 20, 45, tzinfo=UTC),
        [BiddingZone.DK2],
    )
    assert prices[0].imbalance_price_eur == 50.0
    assert prices[0].mfrr_up_price_eur == 50.0
    assert prices[0].mfrr_down_price_eur == 50.0
    assert prices[0].dominating_direction is Direction.NONE


def test_dk_all_zero_prices_with_an_activation_direction_stay_real() -> None:
    # A settled zero-price interval still reports its regulation direction; only the
    # placeholder also has mainDirRegPowerPerMBA == 0.
    record = {**DK_PLACEHOLDER, "mainDirRegPowerPerMBA": 1.0}
    prices = esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(
        datetime(2026, 10, 18, 20, 30, tzinfo=UTC),
        datetime(2026, 10, 18, 20, 45, tzinfo=UTC),
        [BiddingZone.DK2],
    )
    assert prices[0].imbalance_price_eur == 0.0
    assert prices[0].dominating_direction is Direction.UP


def test_all_zero_row_is_real_for_non_dk_zones() -> None:
    record = {**DK_PLACEHOLDER, "mba": "SE3"}
    prices = esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(
        datetime(2026, 10, 18, 20, 30, tzinfo=UTC),
        datetime(2026, 10, 18, 20, 45, tzinfo=UTC),
        [BiddingZone.SE3],
    )
    assert prices[0].imbalance_price_eur == 0.0
    assert prices[0].dominating_direction is Direction.NONE


# --- Hourly and quarter-hour resolutions -------------------------------------------------------


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

    prices = esett_client(handler).imbalance_prices(
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
    prices = esett_client(lambda _: httpx.Response(200, json=records)).imbalance_prices(
        start, start + timedelta(hours=3), [BiddingZone.SE3]
    )
    assert [p.start for p in prices] == [start + i * INTERVAL for i in range(12)]
    assert [p.resolution for p in prices] == [timedelta(hours=1)] * 8 + [INTERVAL] * 4


def test_hourly_only_rows_after_the_switch_raise() -> None:
    # A payload of on-the-hour rows in the 15-minute era is an hourly cadence,
    # not intervals to emit as single 15-minute records.
    records = [
        {**PUBLISHED, "timestampUTC": "2026-01-15T00:00:00Z"},
        {**PUBLISHED, "timestampUTC": "2026-01-15T01:00:00Z"},
    ]
    with pytest.raises(SourceError, match="on the hour only"):
        esett_client(lambda _: httpx.Response(200, json=records)).imbalance_prices(
            START, START + timedelta(hours=2), [BiddingZone.SE3]
        )


def test_hour_rows_do_not_raise_when_a_quarter_row_is_present() -> None:
    records = [
        {**PUBLISHED, "timestampUTC": "2026-01-15T00:00:00Z"},
        {**PUBLISHED, "timestampUTC": "2026-01-15T01:00:00Z"},
        {**PUBLISHED, "timestampUTC": "2026-01-15T01:15:00Z"},
    ]
    prices = esett_client(lambda _: httpx.Response(200, json=records)).imbalance_prices(
        START, START + timedelta(hours=2), [BiddingZone.SE3]
    )
    assert [p.start for p in prices] == [
        START,
        START + timedelta(hours=1),
        START + timedelta(hours=1, minutes=15),
    ]
    assert all(p.resolution == INTERVAL for p in prices)


def test_a_single_hourly_row_does_not_raise() -> None:
    # One isolated hour cannot be told apart from a 15-minute interval.
    prices = esett_client(lambda _: httpx.Response(200, json=[PUBLISHED])).imbalance_prices(
        START, END
    )
    assert [p.start for p in prices] == [START]
    assert prices[0].resolution == INTERVAL


# --- Parsing and request shape -----------------------------------------------------------------


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
    prices = esett_client(
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
    price = esett_client(lambda _: httpx.Response(200, json=[PUBLISHED])).imbalance_prices(
        START, END
    )[0]
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


def test_raw_is_private_to_each_record_and_expanded_quarter() -> None:
    hourly = [{**PUBLISHED, "timestampUTC": "2023-05-21T20:00:00Z", "extra": {"nested": [1]}}]
    quarters = [
        {**PUBLISHED, "timestampUTC": stamp, "extra": {"nested": [1]}}
        for stamp in ("2026-01-15T00:00:00Z", "2026-01-15T00:15:00Z")
    ]
    hour = datetime(2023, 5, 21, 20, tzinfo=UTC)
    expanded = esett_client(lambda _: httpx.Response(200, json=hourly)).imbalance_prices(
        hour, hour + timedelta(hours=1), [BiddingZone.SE3]
    )
    latest = esett_client(lambda _: httpx.Response(200, json=quarters)).imbalance_prices(
        START, START + 2 * INTERVAL, [BiddingZone.SE3]
    )
    raws = [record.raw for record in (*expanded, *latest)]
    assert len(raws) == 6
    assert len({id(raw) for raw in raws}) == len(raws)
    assert len({id(raw["extra"]) for raw in raws}) == len(raws)
    assert len({id(raw["extra"]["nested"]) for raw in raws}) == len(raws)
    raws[0]["extra"]["nested"].append(2)
    assert all(raw["extra"]["nested"] == [1] for raw in raws[1:])
    assert hourly[0]["extra"]["nested"] == [1]


def test_sends_utc_window_repeated_codes_and_custom_base_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    http = http_client(handler)
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

    esett_client(handler).imbalance_prices(START, END)
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


def test_filters_subinterval_start_and_preserves_exclusive_end_precision() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {**PUBLISHED, "timestampUTC": START.isoformat()},
                {**PUBLISHED, "timestampUTC": (START + INTERVAL).isoformat()},
            ],
        )

    prices = esett_client(handler).imbalance_prices(
        START + timedelta(microseconds=1), START + INTERVAL + timedelta(microseconds=1)
    )
    assert [p.start for p in prices] == [START + INTERVAL]
    assert seen[0].url.params["start"] == "2026-01-15T00:00:00.000Z"
    assert seen[0].url.params["end"] == "2026-01-15T00:15:00.001Z"


def test_end_rounds_up_to_the_next_millisecond() -> None:
    # A sub-millisecond tail (not a whole millisecond) must still widen the
    # query end so an interval starting inside it is fetched.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    esett_client(handler).imbalance_prices(START, START + INTERVAL + timedelta(microseconds=500))
    assert seen[0].url.params["end"] == "2026-01-15T00:15:00.001Z"


def test_purchase_only_price_is_kept_not_dropped() -> None:
    record = {**PUBLISHED, "imblSalesPrice": None, "imblPurchasePrice": 33.5}
    price = esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)[
        0
    ]
    assert price.imbalance_price_eur == 33.5
    assert price.raw["imblSalesPrice"] is None


def test_dk_all_zero_prices_with_a_null_direction_are_the_unpublished_placeholder() -> None:
    record = {**DK_PLACEHOLDER, "mainDirRegPowerPerMBA": None}
    prices = esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(
        datetime(2026, 10, 18, 20, 30, tzinfo=UTC),
        datetime(2026, 10, 18, 20, 45, tzinfo=UTC),
        [BiddingZone.DK2],
    )
    assert prices[0].imbalance_price_eur is None
    assert prices[0].dominating_direction is None


def test_rejects_sales_purchase_disagreement_on_an_all_float_row() -> None:
    record = {**PUBLISHED, "imblPurchasePrice": 22.0}
    with pytest.raises(SourceError, match="esett: single-price model violated"):
        esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(START, END)


def test_dk_all_zero_prices_with_a_down_direction_stay_real() -> None:
    record = {**DK_PLACEHOLDER, "mainDirRegPowerPerMBA": -1.0}
    prices = esett_client(lambda _: httpx.Response(200, json=[record])).imbalance_prices(
        datetime(2026, 10, 18, 20, 30, tzinfo=UTC),
        datetime(2026, 10, 18, 20, 45, tzinfo=UTC),
        [BiddingZone.DK2],
    )
    assert prices[0].imbalance_price_eur == 0.0
    assert prices[0].dominating_direction is Direction.DOWN
