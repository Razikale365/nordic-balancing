import gzip
import json
import os
import traceback
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

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


def test_pages_follow_page_one_last_page_despite_inconsistent_later_metadata() -> None:
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
def test_row_count_must_match_page_one_total(later_rows: list[Any]) -> None:
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
        ({"startTime": "2026-01-15T00:05:00Z"}, "15-minute boundary"),
        ({"startTime": "2026-01-15T00:00:00.000001Z"}, "15-minute boundary"),
        ({"endTime": "invalid"}, "unparseable endTime"),
        ({"endTime": "2026-01-15T00:30:00Z"}, "duration must be 15 minutes"),
        ({"endTime": "2026-01-15T00:00:00Z"}, "duration must be 15 minutes"),
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
        ({"data": [], "pagination": {"total": 0}}, "invalid lastPage"),
        ({"data": [], "pagination": {"total": 0, "lastPage": False}}, "invalid lastPage"),
        ({"data": [None], "pagination": {"total": 1, "lastPage": 1}}, "not a JSON object"),
    ],
)
def test_rejects_malformed_response_shapes(body: object, message: str) -> None:
    with pytest.raises(SourceError, match=message):
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
