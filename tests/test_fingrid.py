import gzip
import json
import os
import traceback
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpcore
import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    FingridClient,
    RateLimitError,
    SourceError,
)
from nordic_balancing.sources import FingridClient as SourceFingridClient

START = datetime(2026, 1, 15, tzinfo=UTC)
END = START + timedelta(hours=1)
KEY = "test-fingrid-secret"
PUBLISHED: dict[str, Any] = {
    "datasetId": 319,
    "startTime": "2026-01-15T00:00:00.000Z",
    "endTime": "2026-01-15T00:15:00.000Z",
    "value": 21,
}
Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def row(dataset: int = 319, quarter: int = 0, value: float | None = 21) -> dict[str, Any]:
    start = START + quarter * INTERVAL
    return {
        "datasetId": dataset,
        "startTime": start.isoformat(),
        "endTime": (start + INTERVAL).isoformat(),
        "value": value,
    }


def hour(dataset: int = 319, offset_hours: int = 0, value: float | None = 21) -> dict[str, Any]:
    start = START + offset_hours * timedelta(hours=1)
    return {
        "datasetId": dataset,
        "startTime": start.isoformat(),
        "endTime": (start + timedelta(hours=1)).isoformat(),
        "value": value,
    }


def page(records: list[Any], total: int | None = None, last_page: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": records,
            "pagination": {
                "total": len(records) if total is None else total,
                "lastPage": last_page,
                "nextPage": None,
            },
        },
    )


def client(handler: Handler, timer: FakeClock | None = None) -> FingridClient:
    timer = timer if timer is not None else FakeClock()
    return FingridClient(
        KEY,
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=timer.sleep,
        clock=timer.clock,
    )


def test_series_sorts_newest_first_and_preserves_null_values() -> None:
    values = client(lambda _: page([row(quarter=1, value=None), PUBLISHED])).series(319, START, END)
    assert values == [(START, 21.0), (START + INTERVAL, None)]
    assert all(stamp.tzinfo is UTC for stamp, _ in values)


def test_compressed_response_is_decoded_once() -> None:
    body = json.dumps({"data": [PUBLISHED], "pagination": {"total": 1, "lastPage": 1}}).encode()
    compressed = gzip.compress(body)
    assert client(
        lambda _: httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
            content=compressed,
        )
    ).series(319, START, END) == [(START, 21.0)]


def test_auth_utc_window_and_custom_url_without_mutating_http() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page([])

    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"custom": "kept"})
    local = timezone(timedelta(hours=2))
    with FingridClient(KEY, http, base_url="https://example.test/api/") as fingrid:
        fingrid.series(319, START.astimezone(local), END.astimezone(local))
    request = seen[0]
    assert request.url.host == "example.test"
    assert request.url.path == "/api/datasets/319/data"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["custom"] == "kept"
    assert request.url.params == httpx.QueryParams(
        startTime="2026-01-15T00:00:00Z",
        endTime="2026-01-15T01:00:00Z",
        pageSize="20000",
        page="1",
    )
    assert KEY not in str(request.url)
    assert "x-api-key" not in http.headers
    assert not http.is_closed


def test_env_key_fallback_and_explicit_key_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINGRID_API_KEY", "environment-key")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-api-key"])
        return page([])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with FingridClient(http=http) as fingrid:
        fingrid.series(319, START, END)
    with FingridClient(KEY, http) as fingrid:
        fingrid.series(319, START, END)
    assert seen == ["environment-key", KEY]


@pytest.mark.parametrize("key", [None, "", "  "])
def test_missing_key_names_env_var(key: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINGRID_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FINGRID_API_KEY"):
        FingridClient(key)


def test_pages_follow_page_one_last_page_despite_inconsistent_later_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nordic_balancing.sources.fingrid.PAGE_SIZE", 1)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        number = request.url.params["page"]
        seen.append(number)
        if number == "1":
            return page([row(quarter=3)], total=4, last_page=3)
        if number == "2":
            return httpx.Response(200, json={"data": [row(quarter=2)], "pagination": {}})
        return httpx.Response(
            200,
            json={"data": [row(quarter=1), row()], "pagination": {"currentPage": 3}},
        )

    assert client(handler).series(319, START, END) == [
        (START + i * INTERVAL, 21.0) for i in range(4)
    ]
    assert seen == ["1", "2", "3"]


@pytest.mark.parametrize("later_rows", [[], [row(), row(quarter=1)]])
def test_row_count_must_match_page_one_total(
    later_rows: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nordic_balancing.sources.fingrid.PAGE_SIZE", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return page([row(quarter=3)], total=2, last_page=2)
        return httpx.Response(200, json={"data": later_rows})

    with pytest.raises(SourceError, match=r"row count .* != page-1 total 2"):
        client(handler).series(319, START, END)


def test_join_uses_only_imbalance_starts_and_preserves_raw() -> None:
    records = {
        319: [row(quarter=2, value=None), PUBLISHED],
        244: [row(244, 0, 76.32), row(244, 1, 40)],
        106: [row(106, 2, None), row(106, 0, -5)],
        369: [row(369, 2, 1), row(369, 0, -1)],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return page(records[int(request.url.path.split("/")[-2])])

    prices = client(handler).imbalance_prices(START, END)
    assert [p.start for p in prices] == [START, START + 2 * INTERVAL]
    first, pending = prices
    assert first.imbalance_price_eur == 21.0
    assert first.mfrr_up_price_eur == 76.32
    assert first.mfrr_down_price_eur == -5.0
    assert first.dominating_direction is Direction.DOWN
    assert pending.imbalance_price_eur is None
    assert pending.mfrr_up_price_eur is None
    assert pending.mfrr_down_price_eur is None
    assert pending.dominating_direction is Direction.UP
    assert first.raw == {
        str(dataset): rows[-1] for dataset, rows in records.items() if dataset != 244
    } | {"244": records[244][0]}
    assert set(pending.raw) == {"319", "106", "369"}
    for price in prices:
        assert price.zone is BiddingZone.FI
        assert price.source == "fingrid"
        assert price.resolution == INTERVAL
        assert price.imbalance_price_dkk is None
        assert price.spot_price_eur is None
        assert price.satisfied_demand_mw is None
        assert price.afrr_up_vwa_eur is None
        assert price.afrr_down_vwa_eur is None
    with pytest.raises(TypeError):
        first.raw["319"] = {}  # type: ignore[index]
    assert "raw=" not in repr(first)
    assert KEY not in repr(first)


@pytest.mark.parametrize(
    "value, expected", [(-1, Direction.DOWN), (0, Direction.NONE), (1, Direction.UP), (None, None)]
)
def test_direction_mapping_and_missing_components(
    value: float | None, expected: Direction | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        dataset = int(request.url.path.split("/")[-2])
        return page([row(dataset, value=value)] if dataset in (319, 369) else [])

    price = client(handler).imbalance_prices(START, END)[0]
    assert price.dominating_direction is expected
    assert price.mfrr_up_price_eur is None
    assert price.mfrr_down_price_eur is None


@pytest.mark.parametrize("value", [2, 0.5, -2])
def test_invalid_direction(value: float) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        dataset = int(request.url.path.split("/")[-2])
        return page([row(dataset, value=value if dataset == 369 else 21)])

    with pytest.raises(SourceError, match="unknown dominating direction"):
        client(handler).imbalance_prices(START, END)


def test_throttles_across_queries_and_accounts_for_elapsed_time() -> None:
    timer = FakeClock()
    times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        times.append(timer.now)
        return page([])

    fingrid = client(handler, timer)
    fingrid.series(319, START, END)
    timer.now += 0.5
    fingrid.series(319, START, END)
    timer.now += 3
    fingrid.imbalance_prices(START, END)
    assert times == [0, 2, 5, 7, 9, 11]
    assert timer.sleeps == [1.5, 2, 2, 2]


def test_retries_use_shared_helper_and_pace_every_attempt() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "0.5"}),
            httpx.Response(503, headers={"Retry-After": "invalid"}),
            page([PUBLISHED]),
        ]
    )
    timer = FakeClock()
    times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == KEY
        times.append(timer.now)
        return next(responses)

    assert client(handler, timer).series(319, START, END) == [(START, 21.0)]
    assert timer.sleeps == [0.5, 1.5, 10]
    assert times == [0, 2, 12]


@pytest.mark.parametrize("status", [429, 503])
def test_bounded_retry_budget(status: int) -> None:
    calls: list[httpx.Request] = []
    timer = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitError) as caught:
        client(handler, timer).series(319, START, END)
    assert caught.value.source == "fingrid"
    assert caught.value.retry_after == 3.0
    assert len(calls) == 4
    assert timer.sleeps == [3.0] * 3


@pytest.mark.parametrize(
    "override, message",
    [
        ({"datasetId": True}, "unexpected datasetId"),
        ({"datasetId": 244}, "unexpected datasetId"),
        ({"datasetId": 319.0}, "unexpected datasetId"),
        ({"startTime": None}, "startTime is missing"),
        ({"startTime": KEY}, "unparseable startTime"),
        ({"startTime": "2026-01-15T00:00:00"}, "must be UTC"),
        ({"startTime": "2026-01-15T01:00:00+01:00"}, "must be UTC"),
        ({"startTime": "2026-01-15T01:00:00-01:00"}, "must be UTC"),
        ({"startTime": "2026-01-15T00:00:00-00:00"}, "must be UTC"),
        ({"startTime": "2026-01-15T00:00:00+00"}, "must be UTC"),
        ({"startTime": "2026-01-15T00:00:00+0000"}, "must be UTC"),
        ({"startTime": "2026-01-15T00:00:00+00:00:00"}, "must be UTC"),
        ({"startTime": "2026-01-15T00:05:00Z"}, "15-minute boundary"),
        ({"startTime": "2026-01-15T00:00:30Z"}, "15-minute boundary"),
        ({"startTime": "2026-01-15T00:00:00.000001Z"}, "15-minute boundary"),
        ({"endTime": "invalid"}, "unparseable endTime"),
        ({"endTime": "2026-01-15T00:30:00Z"}, "duration must be 15 or 60 minutes"),
        ({"endTime": "2026-01-15T00:00:00Z"}, "duration must be 15 or 60 minutes"),
        ({"value": KEY}, "not a number"),
        ({"value": True}, "not a number"),
        ({"value": []}, "not a number"),
    ],
)
def test_rejects_malformed_records(override: dict[str, Any], message: str) -> None:
    with pytest.raises(SourceError, match=message) as caught:
        client(lambda _: page([{**PUBLISHED, **override}])).series(319, START, END)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**400])
def test_rejects_non_finite_values(value: int | float) -> None:
    body = json.dumps(
        {"data": [{**PUBLISHED, "value": value}], "pagination": {"total": 1, "lastPage": 1}}
    )
    with pytest.raises(SourceError, match="not finite"):
        client(lambda _: httpx.Response(200, text=body)).series(319, START, END)


@pytest.mark.parametrize(
    "body, message",
    [
        ([], "not a JSON object"),
        ({}, "no 'data' list"),
        ({"data": None}, "no 'data' list"),
        ({"data": []}, "no pagination object"),
        ({"data": [], "pagination": {"total": True, "lastPage": 1}}, "invalid total"),
        ({"data": [], "pagination": {"total": -1, "lastPage": 1}}, "invalid total"),
        ({"data": [], "pagination": {"total": 1}}, "invalid lastPage"),
        ({"data": [], "pagination": {"total": 1, "lastPage": False}}, "invalid lastPage"),
        ({"data": [], "pagination": {"total": 1, "lastPage": 0}}, "invalid lastPage"),
        ({"data": [None], "pagination": {"total": 1, "lastPage": 1}}, "not a JSON object"),
    ],
)
def test_rejects_malformed_response_shapes(body: object, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        client(lambda _: httpx.Response(200, json=body)).series(319, START, END)


@pytest.mark.parametrize(
    "pagination",
    [
        {"total": 0, "lastPage": 1},
        {"total": 0, "lastPage": 0},
        {"total": 0},
        {"total": 0, "lastPage": None},
        {"total": 0, "lastPage": False},
    ],
)
def test_a_window_with_no_rows_returns_empty(pagination: dict[str, Any]) -> None:
    values = client(
        lambda _: httpx.Response(200, json={"data": [], "pagination": pagination})
    ).series(319, START, END)
    assert values == []


def test_rows_reported_with_a_zero_total_still_raise() -> None:
    body = {"data": [PUBLISHED], "pagination": {"total": 0}}
    with pytest.raises(SourceError, match="!= page-1 total 0"):
        client(lambda _: httpx.Response(200, json=body)).series(319, START, END)


@pytest.mark.parametrize(
    "rows, message",
    [
        ([{k: v for k, v in PUBLISHED.items() if k != "value"}], "value is missing"),
        ([PUBLISHED, PUBLISHED], "duplicate"),
        ([row(quarter=4)], "outside requested window"),
    ],
)
def test_rejects_missing_value_duplicate_and_out_of_window_rows(
    rows: list[Any], message: str
) -> None:
    with pytest.raises(SourceError, match=message):
        client(lambda _: page(rows)).series(319, START, END)


def test_partial_window_filters_starts_without_losing_final_quarter() -> None:
    start = START + timedelta(minutes=5, microseconds=1)
    end = START + INTERVAL + timedelta(microseconds=1)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page([row(quarter=1), row()])

    assert client(handler).series(319, start, end) == [(START + INTERVAL, 21.0)]
    assert seen[0].url.params["endTime"] == "2026-01-15T00:30:00Z"


@pytest.mark.parametrize(
    "response, message",
    [
        (httpx.Response(401, text=KEY), "fingrid: HTTP 401"),
        (httpx.Response(200, text=KEY), "is not JSON"),
    ],
)
def test_errors_and_tracebacks_do_not_leak_key(response: httpx.Response, message: str) -> None:
    fingrid = client(lambda _: response)
    with pytest.raises(SourceError, match=message) as caught:
        fingrid.series(319, START, END)
    assert KEY not in str(caught.value)
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert KEY not in repr(fingrid)


def test_transport_error_does_not_leak_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(KEY, request=request)

    with pytest.raises(SourceError, match="request failed") as caught:
        client(handler).series(319, START, END)
    assert KEY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "dataset, start, end, message",
    [
        (True, START, END, "dataset_id"),
        (0, START, END, "dataset_id"),
        (1.5, START, END, "dataset_id"),
        (319, START.replace(tzinfo=None), END, "timezone-aware"),
        (319, START, END.replace(tzinfo=None), "timezone-aware"),
        (319, END, START, "end must be after start"),
        (319, START, START, "end must be after start"),
    ],
)
def test_bad_arguments_send_no_request(
    dataset: Any, start: datetime, end: datetime, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("bad arguments must not send a request")

    with pytest.raises(ValueError, match=message):
        client(handler).series(dataset, start, end)


@pytest.mark.parametrize("retries", [-1, True, 1.5])
def test_rejects_bad_retry_budget(retries: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        FingridClient(KEY, max_retries=retries)


@pytest.mark.parametrize("interval", [-1, True, float("nan"), float("inf"), "2"])
def test_rejects_bad_min_interval(interval: Any) -> None:
    with pytest.raises(ValueError, match="min_interval"):
        FingridClient(KEY, min_interval=interval)


def test_zero_retries_does_not_sleep() -> None:
    timer = FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429)))
    with (
        FingridClient(KEY, http, max_retries=0, sleep=timer.sleep, clock=timer.clock) as fingrid,
        pytest.raises(RateLimitError),
    ):
        fingrid.series(319, START, END)
    assert timer.sleeps == []


def test_context_manager_closes_only_owned_client_and_exports() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _: page([])))
    with FingridClient(KEY, http):
        pass
    assert not http.is_closed
    with FingridClient(KEY) as owned:
        assert not owned._http.is_closed
    assert owned._http.is_closed
    assert owned._authenticated_http.is_closed
    owned.close()
    assert SourceFingridClient is FingridClient


def test_hourly_records_expand_to_quarter_starts_in_series() -> None:
    values = client(lambda _: page([hour(value=-8.55)])).series(319, START, END)
    assert values == [(START + i * INTERVAL, -8.55) for i in range(4)]


def test_hourly_records_expand_in_imbalance_prices_with_hourly_resolution() -> None:
    records = {
        319: [hour(319, value=-8.55)],
        244: [hour(244, value=40)],
        106: [hour(106, value=2)],
        369: [hour(369, value=-1)],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return page(records[int(request.url.path.split("/")[-2])])

    prices = client(handler).imbalance_prices(START, END)
    assert [p.start for p in prices] == [START + i * INTERVAL for i in range(4)]
    for price in prices:
        assert price.resolution == timedelta(hours=1)
        assert price.imbalance_price_eur == -8.55
        assert price.mfrr_up_price_eur == 40
        assert price.mfrr_down_price_eur == 2
        assert price.dominating_direction is Direction.DOWN
        assert price.raw["319"]["endTime"] == "2026-01-15T01:00:00+00:00"


def test_mixed_resolutions_in_one_response() -> None:
    rows = [hour(319, 0, -8.55)] + [row(319, q, value=q * 10.0) for q in range(4, 8)]
    values = client(lambda _: page(rows)).series(319, START, START + timedelta(hours=2))
    assert values == [(START + i * INTERVAL, -8.55) for i in range(4)] + [
        (START + i * INTERVAL, i * 10.0) for i in range(4, 8)
    ]


def test_finer_resolution_wins_overlapping_quarters_after_the_switch() -> None:
    # From 2025-03-18T23:00Z pricing is 15-minute: the quarter record wins.
    rows = [hour(319, 0, 100), row(319, 1, 7)]
    values = client(lambda _: page(rows)).series(319, START, END)
    assert values == [(START, 100.0), (START + INTERVAL, 7.0)] + [
        (START + i * INTERVAL, 100.0) for i in (2, 3)
    ]


def _at(dataset: int, start: datetime, minutes: int, value: float) -> dict[str, Any]:
    return {
        "datasetId": dataset,
        "startTime": start.isoformat(),
        "endTime": (start + timedelta(minutes=minutes)).isoformat(),
        "value": value,
    }


def test_hourly_record_wins_before_the_switch_and_loser_is_kept() -> None:
    # Live 2025-03-16T09:00Z: hourly 104 (eSett settled 104), quarters 107.
    start = datetime(2025, 3, 16, 9, tzinfo=UTC)
    rows = {
        dataset: [_at(dataset, start, 60, 104)]
        + [_at(dataset, start + q * INTERVAL, 15, 107) for q in (0, 1, 2, 3)]
        for dataset in (319, 244, 106, 369)
    }
    rows[369] = [_at(369, start, 60, 1)]

    def handler(request: httpx.Request) -> httpx.Response:
        return page(rows[int(request.url.path.split("/")[-2])])

    fingrid = client(handler)
    prices = fingrid.imbalance_prices(start, start + timedelta(hours=1))
    assert [p.imbalance_price_eur for p in prices] == [104.0] * 4
    assert all(p.resolution == timedelta(hours=1) for p in prices)
    assert [r["value"] for r in prices[1].raw["319_superseded"]] == [107]
    assert "369_superseded" not in prices[1].raw
    values = fingrid.series(319, start, start + timedelta(hours=1))
    assert values == [(start + q * INTERVAL, 104.0) for q in range(4)]


def test_quarter_record_wins_at_and_after_the_switch() -> None:
    start = datetime(2025, 3, 18, 23, tzinfo=UTC)
    rows = [_at(319, start, 60, 50), _at(319, start + INTERVAL, 15, 60)]
    values = client(lambda _: page(rows)).series(319, start, start + timedelta(hours=1))
    assert values == [(start, 50.0), (start + INTERVAL, 60.0)] + [
        (start + q * INTERVAL, 50.0) for q in (2, 3)
    ]


def test_quarter_record_wins_at_the_switch_instant_and_loser_is_kept() -> None:
    # At exactly 2025-03-18T23:00Z the 15-minute record is already
    # authoritative: the hourly record covering that start loses to it and is
    # kept as superseded.
    start = datetime(2025, 3, 18, 23, tzinfo=UTC)
    hourly = _at(319, start, 60, 50)

    def handler(request: httpx.Request) -> httpx.Response:
        dataset = int(request.url.path.split("/")[-2])
        return page([hourly, _at(319, start, 15, 7)] if dataset == 319 else [])

    fingrid = client(handler)
    prices = fingrid.imbalance_prices(start, start + timedelta(hours=1))
    assert [p.imbalance_price_eur for p in prices] == [7.0] + [50.0] * 3
    assert prices[0].resolution == INTERVAL
    assert [p.resolution for p in prices[1:]] == [timedelta(hours=1)] * 3
    assert prices[0].raw["319_superseded"] == [hourly]
    assert all("319_superseded" not in p.raw for p in prices[1:])
    assert fingrid.series(319, start, start + timedelta(hours=1)) == [(start, 7.0)] + [
        (start + q * INTERVAL, 50.0) for q in (1, 2, 3)
    ]


def test_hourly_record_wins_overlap_in_the_hour_before_the_switch() -> None:
    # 2025-03-18T22:00Z is still inside the hourly era: the hourly value wins
    # every quarter it covers, even when a 15-minute record was also published.
    start = datetime(2025, 3, 18, 22, tzinfo=UTC)
    rows = [_at(319, start, 60, 50), _at(319, start + INTERVAL, 15, 60)]
    values = client(lambda _: page(rows)).series(319, start, start + timedelta(hours=1))
    assert values == [(start + q * INTERVAL, 50.0) for q in range(4)]


def test_same_start_in_both_resolutions_is_not_a_duplicate() -> None:
    start = datetime(2025, 3, 16, 9, tzinfo=UTC)
    rows = [_at(319, start, 60, 104), _at(319, start, 15, 107)]
    values = client(lambda _: page(rows)).series(319, start, start + INTERVAL)
    assert values == [(start, 104.0)]


def test_same_start_and_resolution_twice_is_a_duplicate() -> None:
    start = datetime(2025, 3, 16, 9, tzinfo=UTC)
    rows = [_at(319, start, 60, 104), _at(319, start, 60, 104)]
    with pytest.raises(SourceError, match="duplicate"):
        client(lambda _: page(rows)).series(319, start, start + INTERVAL)


def test_query_start_floors_to_the_hour_and_filters_to_window() -> None:
    seen: list[httpx.Request] = []
    start = START + timedelta(minutes=20)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page([hour(319, 0, 5)])

    values = client(handler).series(319, start, END)
    assert seen[0].url.params["startTime"] == "2026-01-15T00:00:00Z"
    assert values == [(START + i * INTERVAL, 5.0) for i in (2, 3)]


@pytest.mark.live
def test_live_hourly_imbalance_prices_expand_to_quarters() -> None:
    if not os.environ.get("FINGRID_API_KEY"):
        pytest.skip("FINGRID_API_KEY is not set and no key was found in the repo .env")
    start = datetime(2025, 3, 10, tzinfo=UTC)
    with FingridClient() as fingrid:
        prices = fingrid.imbalance_prices(start, start + timedelta(hours=2))
    assert len(prices) == 8
    assert [p.start for p in prices] == [start + i * INTERVAL for i in range(8)]
    assert all(p.resolution == timedelta(hours=1) for p in prices)
    assert [p.imbalance_price_eur for p in prices] == [-8.55] * 4 + [-17.06] * 4


@pytest.mark.live
def test_live_one_hour_of_fi_imbalance_prices() -> None:
    if not os.environ.get("FINGRID_API_KEY"):
        pytest.skip("FINGRID_API_KEY is not set and no key was found in the repo .env")
    with FingridClient() as fingrid:
        prices = fingrid.imbalance_prices(START, END)
    assert len(prices) == 4
    assert [p.start for p in prices] == [START + i * INTERVAL for i in range(4)]
    assert all(p.zone is BiddingZone.FI for p in prices)
    assert all(p.resolution == INTERVAL for p in prices)
    assert all(p.imbalance_price_eur is not None for p in prices)


@pytest.mark.live
def test_live_overlap_hour_uses_the_hourly_value_settled_by_esett() -> None:
    # 2025-03-16T09:00Z: Fingrid published hourly 104 and quarters 107; eSett settled 104.
    if not os.environ.get("FINGRID_API_KEY"):
        pytest.skip("FINGRID_API_KEY is not set and no key was found in the repo .env")
    start = datetime(2025, 3, 16, 9, tzinfo=UTC)
    with FingridClient() as fingrid:
        prices = fingrid.imbalance_prices(start, start + timedelta(hours=1))
    assert [p.imbalance_price_eur for p in prices] == [104.0] * 4
    assert any("319_superseded" in p.raw for p in prices)


def test_other_datasets_reject_hourly_records() -> None:
    # Hourly expansion is only defined for the imbalance price/direction datasets;
    # repeating an hourly value of, e.g., an energy dataset would misstate it.
    with pytest.raises(SourceError, match="dataset 75 returned 60-minute records"):
        client(lambda _: page([hour(75, 0, 400)])).series(75, START, END)


def test_other_datasets_reject_overlapping_granularities() -> None:
    rows = [hour(75, 0, 400), row(75, 1, 90)]
    with pytest.raises(SourceError, match="60-minute records"):
        client(lambda _: page(rows)).series(75, START, END)


def test_other_datasets_keep_15_minute_records() -> None:
    rows = [row(75, q, value=float(q)) for q in range(4)]
    values = client(lambda _: page(rows)).series(75, START, END)
    assert values == [(START + q * INTERVAL, float(q)) for q in range(4)]


@pytest.mark.parametrize("dataset", [319, 244, 106, 369])
def test_imbalance_datasets_keep_their_hourly_policy(dataset: int) -> None:
    values = client(lambda _: page([hour(dataset, 0, -1)])).series(dataset, START, END)
    assert values == [(START + q * INTERVAL, -1.0) for q in range(4)]


# --- Audit 2026-10-09 (baseline v0.1.0a3) -------------------------------------------


@pytest.mark.parametrize("minute", [15, 30, 45])
def test_rejects_hourly_records_not_on_the_hour(minute: int) -> None:
    start = START + timedelta(minutes=minute)
    record = {
        "datasetId": 319,
        "startTime": start.isoformat(),
        "endTime": (start + timedelta(hours=1)).isoformat(),
        "value": 2,
    }
    with pytest.raises(SourceError, match="60-minute record is not on the hour"):
        client(lambda _: page([hour(value=1), record])).series(319, START, END + timedelta(hours=1))


def test_api_key_is_not_sent_to_a_redirect_target() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "data.fingrid.fi":
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/steal"})
        return page([PUBLISHED])

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fingrid = FingridClient(KEY, http, sleep=lambda _: None, clock=lambda: 0.0)
    with pytest.raises(SourceError):
        fingrid.series(319, START, END)
    assert hosts == ["data.fingrid.fi"]


def test_strips_surrounding_whitespace_from_key_and_redacts_stripped_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers["x-api-key"])
        return httpx.Response(401)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with FingridClient(f"  {KEY}\n", http, sleep=lambda _: None, clock=lambda: 0.0) as fingrid:
        assert fingrid._api_key == KEY
        with pytest.raises(SourceError) as caught:
            fingrid._get(f"/datasets/319/data/{KEY}", {})
        assert KEY not in str(caught.value)
        assert "[REDACTED_SECRET]" in str(caught.value)

    monkeypatch.setenv("FINGRID_API_KEY", f"\t{KEY}  \r\n")
    with FingridClient(http=http, sleep=lambda _: None, clock=lambda: 0.0) as from_env:
        assert from_env._api_key == KEY
        with pytest.raises(SourceError) as caught_env:
            from_env._get(f"/datasets/319/data/{KEY}", {})
        assert KEY not in str(caught_env.value)
    assert seen_headers == [KEY, KEY]


@pytest.mark.parametrize(
    "bad_key",
    [
        f"{KEY}-ä",
        f"{KEY} extra",
        f"{KEY}\nextra",
        f"{KEY}\textra",
        f"{KEY}\x01extra",
        f"{KEY}\x7fextra",
    ],
)
def test_rejects_non_ascii_and_internal_whitespace_keys_without_echoing_key(
    bad_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="printable ASCII") as caught:
        FingridClient(bad_key)
    assert KEY not in str(caught.value)
    assert bad_key.strip() not in str(caught.value)

    monkeypatch.setenv("FINGRID_API_KEY", bad_key)
    with pytest.raises(ValueError, match="printable ASCII") as caught_env:
        FingridClient()
    assert KEY not in str(caught_env.value)
    assert bad_key.strip() not in str(caught_env.value)


def test_error_response_with_corrupt_encoding_is_not_decoded_and_retries() -> None:
    class TrackedStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        def __iter__(self) -> Iterator[bytes]:
            self.reads += 1
            yield b"Service Unavailable"

        def close(self) -> None:
            self.closed = True

    timer = FakeClock()
    streams: list[TrackedStream] = []

    def rate_limit_handler(request: httpx.Request) -> httpx.Response:
        stream = TrackedStream()
        streams.append(stream)
        return httpx.Response(
            503,
            headers={"Content-Encoding": "gzip", "Retry-After": "1"},
            stream=stream,
        )

    with pytest.raises(RateLimitError) as caught:
        client(rate_limit_handler, timer).series(319, START, END)
    assert caught.value.source == "fingrid"
    assert caught.value.retry_after == 1.0
    assert len(streams) == 4
    assert all(stream.reads == 0 and stream.closed for stream in streams)
    assert timer.sleeps == [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]

    transient_attempts = 0
    transient_timer = FakeClock()

    def transient_handler(request: httpx.Request) -> httpx.Response:
        nonlocal transient_attempts
        transient_attempts += 1
        if transient_attempts == 1:
            return httpx.Response(
                502,
                headers={"Content-Encoding": "gzip"},
                stream=httpx.ByteStream(b"Bad Gateway"),
            )
        return page([PUBLISHED])

    assert client(transient_handler, transient_timer).series(319, START, END) == [(START, 21.0)]
    assert transient_attempts == 2


def test_non_hourly_dataset_floors_query_start_to_quarter_for_quarter_and_mid_hour_starts() -> None:
    seen_quarter: list[httpx.Request] = []
    quarter_start = START + INTERVAL
    quarter_end = START + 2 * INTERVAL

    def quarter_handler(request: httpx.Request) -> httpx.Response:
        seen_quarter.append(request)
        return page([row(75, 1, 25)])

    assert client(quarter_handler).series(75, quarter_start, quarter_end) == [(quarter_start, 25.0)]
    assert seen_quarter[0].url.params["startTime"] == "2026-01-15T00:15:00Z"

    with pytest.raises(SourceError, match="outside requested window"):
        client(lambda _: page([row(75, 0, 99), row(75, 1, 25)])).series(
            75, quarter_start, quarter_end
        )

    seen_mid_hour: list[httpx.Request] = []
    mid_hour_start = START + timedelta(minutes=20)
    mid_hour_end = START + 3 * INTERVAL

    def mid_hour_handler(request: httpx.Request) -> httpx.Response:
        seen_mid_hour.append(request)
        return page([row(75, 1, 10), row(75, 2, 25)])

    assert client(mid_hour_handler).series(75, mid_hour_start, mid_hour_end) == [
        (START + 2 * INTERVAL, 25.0)
    ]
    assert seen_mid_hour[0].url.params["startTime"] == "2026-01-15T00:15:00Z"

    with pytest.raises(SourceError, match="outside requested window"):
        client(lambda _: page([row(75, 0, 99), row(75, 2, 25)])).series(
            75, mid_hour_start, mid_hour_end
        )


def test_pagination_rejects_empty_short_and_overflowing_pages_without_further_requests() -> None:
    pages_empty_first: list[str] = []

    def empty_first_handler(request: httpx.Request) -> httpx.Response:
        page_num = request.url.params["page"]
        pages_empty_first.append(page_num)
        return page([] if page_num == "1" else [PUBLISHED], total=1, last_page=2)

    with pytest.raises(SourceError, match=r"page 1 ended with row count 0 != page-1 total 1"):
        client(empty_first_handler).series(319, START, END)
    assert pages_empty_first == ["1"]

    pages_short_first: list[str] = []

    def short_first_handler(request: httpx.Request) -> httpx.Response:
        page_num = request.url.params["page"]
        pages_short_first.append(page_num)
        return page([PUBLISHED] if page_num == "1" else [row(quarter=1)], total=2, last_page=2)

    with pytest.raises(SourceError, match=r"page 1 ended with row count 1 != page-1 total 2"):
        client(short_first_handler).series(319, START, END)
    assert pages_short_first == ["1"]

    pages_empty_after_total: list[str] = []

    def empty_after_total_handler(request: httpx.Request) -> httpx.Response:
        page_num = request.url.params["page"]
        pages_empty_after_total.append(page_num)
        return page([PUBLISHED] if page_num == "1" else [], total=1, last_page=3)

    with pytest.raises(SourceError, match=r"page 2 returned no rows"):
        client(empty_after_total_handler).series(319, START, END)
    assert pages_empty_after_total == ["1", "2"]

    pages_overflow: list[str] = []

    def overflow_handler(request: httpx.Request) -> httpx.Response:
        page_num = request.url.params["page"]
        pages_overflow.append(page_num)
        return page([PUBLISHED, row(quarter=1)], total=1, last_page=2)

    with pytest.raises(SourceError, match=r"row count 2 != page-1 total 1"):
        client(overflow_handler).series(319, START, END)
    assert pages_overflow == ["1"]


def test_full_pages_falling_short_of_page_one_total_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every page is full (no short-page signal), yet fewer rows arrive than the
    # page-1 total: the final count check must still catch the truncation.
    monkeypatch.setattr("nordic_balancing.sources.fingrid.PAGE_SIZE", 1)
    pages = iter([[row(quarter=q)] for q in range(3)])

    def handler(request: httpx.Request) -> httpx.Response:
        return page(next(pages), total=4, last_page=3)

    with pytest.raises(SourceError, match=r"row count 3 != page-1 total 4"):
        client(handler).series(319, START, END)


@pytest.mark.parametrize("padded_key", [f"  {KEY}  ", f"{KEY}\n"])
def test_padded_keys_work_through_http_header_validation(padded_key: str) -> None:
    body = b'{"data":[],"pagination":{"total":0,"lastPage":1}}'
    raw = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    transport = httpx.HTTPTransport()
    transport._pool._network_backend = httpcore.MockBackend([raw])
    with (
        httpx.Client(transport=transport) as http,
        FingridClient(
            padded_key, http, min_interval=0, sleep=lambda _: None, clock=lambda: 0.0
        ) as fingrid,
    ):
        assert fingrid.series(319, START, END) == []


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_corrupt_encoded_http_error_preserves_status_and_retry_budget(status: int) -> None:
    body = b"Service Unavailable"
    raw = (
        f"HTTP/1.1 {status} Error\r\nContent-Encoding: gzip\r\n"
        f"Content-Length: {len(body)}\r\nRetry-After: 1\r\n\r\n"
    ).encode() + body
    transport = httpx.HTTPTransport()
    transport._pool._network_backend = httpcore.MockBackend([raw] * 4)
    sleeps: list[float] = []
    with (
        httpx.Client(transport=transport) as http,
        FingridClient(
            KEY, http, min_interval=0, max_retries=3, sleep=sleeps.append, clock=lambda: 0.0
        ) as fingrid,
    ):
        if status in (429, 503):
            with pytest.raises(RateLimitError) as caught:
                fingrid.series(319, START, END)
            assert caught.value.source == "fingrid"
            assert caught.value.retry_after == 1.0
        else:
            with pytest.raises(SourceError, match=f"HTTP {status}") as transient:
                fingrid.series(319, START, END)
            assert not isinstance(transient.value, RateLimitError)
    assert sleeps == ([1.0] * 3 if status in (429, 503) else [10.0] * 3)


def test_non_hourly_transition_mock_honours_transmitted_query_window() -> None:
    start = datetime(2025, 3, 14, 23, 15, tzinfo=UTC)
    end = start + INTERVAL
    rows = [_at(75, start - INTERVAL, 60, 100), _at(75, start, 15, 25)]
    seen: list[tuple[datetime, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        low = datetime.fromisoformat(request.url.params["startTime"])
        high = datetime.fromisoformat(request.url.params["endTime"])
        seen.append((low, high))
        return page(
            [record for record in rows if low <= datetime.fromisoformat(record["startTime"]) < high]
        )

    with client(handler) as fingrid:
        assert fingrid.series(75, start, end) == [(start, 25.0)]
    assert seen == [(start, end)]
