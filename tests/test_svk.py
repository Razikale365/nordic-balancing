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
    RateLimitError,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
    SvKClient,
)
from nordic_balancing.sources import SvKClient as SourceSvKClient

RECORD: dict[str, Any] = {
    "_id": 191887,
    "start_time_sweden": "2026-10-08T02:00:00",
    "start_time_utc": "2026-10-08T00:00:00",
    "bidding_zone": "SE3",
    "reserve_product": "mFRRCapacityMarket",
    "reserve_direction": "up",
    "price": 7.2,
    "price_unit": "EUR-MW",
    "volume": 594.0,
    "volume_unit": "MW",
    "soda_hashbyte": "de5b139233baf982f65841f1933de612",
    "soda_identity": 185433,
}
START = datetime(2026, 10, 8, tzinfo=UTC)
END = START + timedelta(hours=1)
RESOURCE_ID = "00000000-0000-0000-0000-000000000001"
Handler = Callable[[httpx.Request], httpx.Response]


def package_show(resource_id: str = RESOURCE_ID) -> dict[str, Any]:
    return {
        "success": True,
        "result": {"resources": [{"id": resource_id, "datastore_active": True}]},
    }


def search(records: list[Any], total: int | None = None) -> dict[str, Any]:
    return {
        "success": True,
        "result": {"total": len(records) if total is None else total, "records": records},
    }


def client(datastore: Handler, sleeps: list[float] | None = None) -> SvKClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(200, json=package_show())
        return datastore(request)

    recorded = sleeps if sleeps is not None else []
    return SvKClient(httpx.Client(transport=httpx.MockTransport(handler)), sleep=recorded.append)


def empty(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=search([]))


def recording(seen: list[httpx.Request]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return empty(request)

    return handler


def serve_text(text: str) -> Handler:
    return lambda _: httpx.Response(200, text=text)


def test_resolves_and_caches_the_datastore_resource_per_product() -> None:
    seen: list[httpx.Request] = []
    resources = {
        "mfrr_capacity_market": "rid-mfrr",
        "afrr_capacity_market": "rid-afrr",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/package_show"):
            dataset = request.url.params["id"]
            return httpx.Response(200, json=package_show(resources[dataset]))
        return httpx.Response(200, json=search([]))

    svk = SvKClient(httpx.Client(transport=httpx.MockTransport(handler)))
    svk.capacity_market(ReserveProduct.MFRR, START, END)
    svk.capacity_market(ReserveProduct.MFRR, START, END)
    svk.capacity_market(ReserveProduct.AFRR, START, END)
    searches = [r for r in seen if r.url.path.endswith("/datastore_search")]
    assert [r.url.params["id"] for r in seen if r.url.path.endswith("/package_show")] == [
        "mfrr_capacity_market",
        "afrr_capacity_market",
    ]
    assert [r.url.params["resource_id"] for r in searches] == [
        "rid-mfrr",
        "rid-mfrr",
        "rid-afrr",
    ]


def test_sends_filters_sort_limit_and_offset() -> None:
    seen: list[httpx.Request] = []
    client(recording(seen)).capacity_market(
        ReserveProduct.MFRR, START, END, [BiddingZone.SE4, BiddingZone.SE1, BiddingZone.SE4]
    )
    params = seen[0].url.params
    assert params["resource_id"] == RESOURCE_ID
    assert json.loads(params["filters"]) == {
        "start_time_utc": ["2026-10-08T00:00:00"],
        "bidding_zone": ["SE1", "SE4"],
    }
    assert params["limit"] == "10000"
    assert params["offset"] == "0"
    assert params["sort"] == "start_time_utc,bidding_zone,reserve_direction"
    assert "Authorization" not in seen[0].headers


def test_expands_hourly_records_and_sorts_by_start_zone_direction() -> None:
    records = [
        {**RECORD, "reserve_direction": "up"},
        {**RECORD, "reserve_direction": "down", "price": 8.0, "volume": 407.0},
    ]
    result = client(lambda _: httpx.Response(200, json=search(records))).capacity_market(
        ReserveProduct.MFRR, START, END, [BiddingZone.SE3]
    )
    assert len(result) == 8
    assert [(r.start, r.direction) for r in result] == [
        (START + i * INTERVAL, direction)
        for i in range(4)
        for direction in (Direction.DOWN, Direction.UP)
    ]
    up = result[1]
    assert up.product is ReserveProduct.MFRR
    assert up.zone is BiddingZone.SE3
    assert up.price_eur_per_mw == 7.2
    assert up.volume_mw == 594.0
    assert up.source == "svk"
    assert up.resolution == timedelta(hours=1)
    assert up.end == up.start + INTERVAL
    assert isinstance(up, ReserveCapacity)
    assert up.raw == RECORD
    with pytest.raises(TypeError):
        up.raw["price"] = 0  # type: ignore[index]
    down = result[0]
    assert down.price_eur_per_mw == 8.0
    assert down.volume_mw == 407.0


def test_trims_expanded_quarters_to_the_requested_window() -> None:
    hour = START
    records = [
        {**RECORD, "start_time_utc": (hour + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00")}
        for i in range(2)
    ]
    result = client(lambda _: httpx.Response(200, json=search(records))).capacity_market(
        ReserveProduct.MFRR,
        hour + timedelta(minutes=20),
        hour + timedelta(hours=1, minutes=40),
        [BiddingZone.SE3],
    )
    assert [r.start for r in result] == [hour + i * INTERVAL for i in range(2, 7)]
    assert all(r.resolution == timedelta(hours=1) for r in result)


def test_queries_floor_of_start_and_hour_containing_a_non_hour_end() -> None:
    seen: list[httpx.Request] = []
    c = client(recording(seen))
    c.capacity_market(ReserveProduct.MFRR, START, END, [BiddingZone.SE3])
    c.capacity_market(
        ReserveProduct.MFRR,
        START + timedelta(minutes=30),
        START + timedelta(hours=1, minutes=30),
        [BiddingZone.SE3],
    )
    assert json.loads(seen[0].url.params["filters"])["start_time_utc"] == ["2026-10-08T00:00:00"]
    assert json.loads(seen[1].url.params["filters"])["start_time_utc"] == [
        "2026-10-08T00:00:00",
        "2026-10-08T01:00:00",
    ]


def test_chunks_hour_filters_at_one_week_per_request() -> None:
    seen: list[httpx.Request] = []
    client(recording(seen)).capacity_market(
        ReserveProduct.MFRR, START, START + timedelta(hours=170), [BiddingZone.SE3]
    )
    hour_lists = [
        json.loads(r.url.params["filters"])["start_time_utc"]
        for r in seen
        if r.url.path.endswith("/datastore_search")
    ]
    assert [len(hours) for hours in hour_lists] == [168, 2]
    assert hour_lists[0][0] == "2026-10-08T00:00:00"
    assert hour_lists[0][-1] == "2026-10-14T23:00:00"
    assert hour_lists[1] == ["2026-10-15T00:00:00", "2026-10-15T01:00:00"]


def test_pages_with_offset_until_rows_reach_total() -> None:
    seen: list[httpx.Request] = []
    pages = {
        "0": search([RECORD], total=3),
        "1": search(
            [
                {**RECORD, "reserve_direction": "down"},
                {**RECORD, "bidding_zone": "SE4"},
            ],
            total=3,
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[request.url.params["offset"]])

    result = client(handler).capacity_market(
        ReserveProduct.MFRR, START, END, [BiddingZone.SE3, BiddingZone.SE4]
    )
    assert [r.url.params["offset"] for r in seen] == ["0", "1"]
    assert len(result) == 12


def test_rejects_an_empty_page_before_total_and_overshooting_total() -> None:
    pages = iter(
        [
            httpx.Response(200, json=search([RECORD], total=2)),
            httpx.Response(200, json=search([], total=2)),
        ]
    )
    with pytest.raises(SourceError, match="ended before reaching total"):
        client(lambda _: next(pages)).capacity_market(
            ReserveProduct.MFRR, START, END, [BiddingZone.SE3]
        )
    over = httpx.Response(200, json=search([RECORD, RECORD], total=1))
    with pytest.raises(SourceError, match="row count 2 != total 1"):
        client(lambda _: over).capacity_market(ReserveProduct.MFRR, START, END, [BiddingZone.SE3])


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"reserve_product": "aFRRCapacityMarket"}, "unexpected reserve_product"),
        ({"reserve_product": None}, "unexpected reserve_product"),
        ({"bidding_zone": "SE2"}, "not a requested bidding_zone"),
        ({"bidding_zone": "DK1"}, "not a requested bidding_zone"),
        ({"bidding_zone": None}, "not a requested bidding_zone"),
        ({"reserve_direction": "side"}, "unknown reserve_direction"),
        ({"reserve_direction": None}, "unknown reserve_direction"),
        ({"reserve_direction": 1}, "unknown reserve_direction"),
        ({"start_time_utc": None}, "missing or not a string"),
        ({"start_time_utc": 5}, "missing or not a string"),
        ({"start_time_utc": "invalid"}, "unparseable start_time_utc"),
        ({"start_time_utc": "2026-10-08T00:00:00Z"}, "must be naive UTC"),
        ({"start_time_utc": "2026-10-08T00:00:00+00:00"}, "must be naive UTC"),
        ({"start_time_utc": "2026-10-08T00:15:00"}, "not on the hour"),
        ({"start_time_utc": "2026-10-08T01:00:00"}, "outside requested chunk"),
        ({"price_unit": "EUR-MWh"}, "unexpected price_unit"),
        ({"price_unit": None}, "unexpected price_unit"),
        ({"volume_unit": "kW"}, "unexpected volume_unit"),
        ({"price": "7.2"}, "price is not a number"),
        ({"price": True}, "price is not a number"),
        ({"volume": []}, "volume is not a number"),
    ],
)
def test_rejects_malformed_records(override: dict[str, Any], message: str) -> None:
    record = {**RECORD, **override}
    with pytest.raises(SourceError, match=message):
        client(lambda _: httpx.Response(200, json=search([record]))).capacity_market(
            ReserveProduct.MFRR, START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rejects_non_finite_price_and_volume(value: str) -> None:
    for column in ("price", "volume"):
        record = json.dumps(search([{**RECORD, column: json.loads(value)}]))
        with pytest.raises(SourceError, match="not finite"):
            client(serve_text(record)).capacity_market(
                ReserveProduct.MFRR, START, END, [BiddingZone.SE3]
            )


@pytest.mark.parametrize("records", [[None], ["bad"]])
def test_rejects_non_object_records(records: list[Any]) -> None:
    with pytest.raises(SourceError, match="not a JSON object"):
        client(lambda _: httpx.Response(200, json=search(records))).capacity_market(
            ReserveProduct.MFRR, START, END, [BiddingZone.SE3]
        )


def test_rejects_duplicate_hour_zone_direction() -> None:
    with pytest.raises(SourceError, match="duplicate record"):
        client(lambda _: httpx.Response(200, json=search([RECORD, RECORD]))).capacity_market(
            ReserveProduct.MFRR, START, END, [BiddingZone.SE3]
        )


def test_allows_null_price_and_volume() -> None:
    record = {**RECORD, "price": None, "volume": None}
    result = client(lambda _: httpx.Response(200, json=search([record]))).capacity_market(
        ReserveProduct.MFRR, START, END, [BiddingZone.SE3]
    )
    assert result[0].price_eur_per_mw is None
    assert result[0].volume_mw is None


@pytest.mark.parametrize("path", ["/package_show", "/datastore_search"])
def test_rejects_unsuccessful_and_malformed_ckan_envelopes(path: str) -> None:
    for body, message in [
        ({"success": False, "error": {"message": "nope"}}, "reported failure"),
        ({"success": True}, "no result object"),
        ([1, 2], "not a JSON object"),
    ]:

        def handler(request: httpx.Request, body: Any = body) -> httpx.Response:
            if request.url.path.endswith(path):
                return httpx.Response(200, json=body)
            if request.url.path.endswith("/package_show"):
                return httpx.Response(200, json=package_show())
            return empty(request)

        svk = SvKClient(httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(SourceError, match=message):
            svk.capacity_market(ReserveProduct.MFRR, START, END, [BiddingZone.SE3])


def test_rejects_package_show_without_a_datastore_resource() -> None:
    bodies = [
        {"success": True, "result": {"resources": []}},
        {"success": True, "result": {"resources": [{"id": "x", "datastore_active": False}]}},
        {"success": True, "result": {"resources": [{"datastore_active": True}]}},
        {"success": True, "result": {}},
    ]
    for body in bodies:

        def handler(request: httpx.Request, body: Any = body) -> httpx.Response:
            if request.url.path.endswith("/package_show"):
                return httpx.Response(200, json=body)
            pytest.fail("datastore_search must not be requested")

        svk = SvKClient(httpx.Client(transport=httpx.MockTransport(handler)))
        with pytest.raises(SourceError, match="svk:"):
            svk.capacity_market(ReserveProduct.MFRR, START, END)


def test_rejects_invalid_total_and_missing_records() -> None:
    for result in [{"total": "x", "records": []}, {"total": -1, "records": []}, {"total": 0}]:

        def handler(request: httpx.Request, result: Any = result) -> httpx.Response:
            if request.url.path.endswith("/package_show"):
                return httpx.Response(200, json=package_show())
            return httpx.Response(200, json={"success": True, "result": result})

        with pytest.raises(SourceError, match="svk:"):
            client(handler).capacity_market(ReserveProduct.MFRR, START, END, [BiddingZone.SE3])


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(400, text="bad request"), "svk: HTTP 400"),
        (httpx.Response(200, text="not json"), "svk: response .* is not JSON"),
    ],
)
def test_source_errors_name_svk(response: httpx.Response, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        client(lambda _: response).capacity_market(ReserveProduct.MFRR, START, END)


def test_wraps_transport_errors_as_source_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(200, json=package_show())
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(SourceError, match="svk: request failed for /datastore_search"):
        client(handler).capacity_market(ReserveProduct.MFRR, START, END)


def test_retries_429_via_sleep_then_succeeds() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2.5"}),
            httpx.Response(200, json=search([RECORD])),
        ]
    )
    sleeps: list[float] = []
    result = client(lambda _: next(responses), sleeps).capacity_market(
        ReserveProduct.MFRR, START, END, [BiddingZone.SE3]
    )
    assert sleeps == [2.5]
    assert len(result) == 4


@pytest.mark.parametrize("status", [429, 503])
def test_retry_budget_is_bounded_and_reports_source(status: int) -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(200, json=package_show())
        return httpx.Response(status, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitError) as caught:
        client(handler, sleeps).capacity_market(ReserveProduct.MFRR, START, END)
    assert caught.value.source == "svk"
    assert caught.value.retry_after == 3.0
    assert sleeps == [3.0] * 3


@pytest.mark.parametrize(
    ("product", "start", "end", "zones", "message"),
    [
        ("mFRR", START, END, None, "ReserveProduct"),
        (ReserveProduct.MFRR, START.replace(tzinfo=None), END, None, "timezone-aware"),
        (ReserveProduct.MFRR, START, END.replace(tzinfo=None), None, "timezone-aware"),
        (ReserveProduct.MFRR, END, START, None, "end must be after start"),
        (ReserveProduct.MFRR, START, START, None, "end must be after start"),
        (ReserveProduct.MFRR, START, END, [], "zones must not be empty"),
        (ReserveProduct.MFRR, START, END, ["SE3"], "BiddingZone"),
        (ReserveProduct.MFRR, START, END, [BiddingZone.DK1], "SE1-SE4"),
        (ReserveProduct.MFRR, START, END, [BiddingZone.SE3, BiddingZone.FI], "SE1-SE4"),
    ],
)
def test_rejects_bad_arguments_without_request(
    product: Any,
    start: datetime,
    end: datetime,
    zones: Any,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("bad arguments must not send an HTTP request")

    c = client(handler)
    with pytest.raises(ValueError, match=message):
        if zones is None:
            c.capacity_market(product, start, end)
        else:
            c.capacity_market(product, start, end, zones)


@pytest.mark.parametrize("retries", [-1, True, 1.5])
def test_rejects_bad_retry_budget(retries: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        SvKClient(max_retries=retries)


def test_converts_non_utc_aware_datetimes() -> None:
    seen: list[httpx.Request] = []
    local = timezone(timedelta(hours=2))
    client(recording(seen)).capacity_market(
        ReserveProduct.MFRR,
        START.astimezone(local),
        END.astimezone(local),
        [BiddingZone.SE3],
    )
    assert json.loads(seen[-1].url.params["filters"])["start_time_utc"] == ["2026-10-08T00:00:00"]


def test_context_manager_closes_only_owned_client_and_exports() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    with SvKClient(http):
        pass
    assert not http.is_closed
    with SvKClient() as owned:
        assert not owned._http.is_closed
    assert owned._http.is_closed
    owned.close()
    assert SourceSvKClient is SvKClient


@pytest.mark.live
def test_live_mfrr_one_hour_all_zones() -> None:
    with SvKClient() as svk:
        result = svk.capacity_market(ReserveProduct.MFRR, START, END)
    assert len(result) == 32
    assert all(r.resolution == timedelta(hours=1) for r in result)
    se3_up = next(r for r in result if r.zone is BiddingZone.SE3 and r.direction is Direction.UP)
    assert se3_up.price_eur_per_mw == 7.2
    assert se3_up.volume_mw == 594.0


@pytest.mark.live
def test_live_afrr_one_hour() -> None:
    with SvKClient() as svk:
        result = svk.capacity_market(ReserveProduct.AFRR, START, END)
    assert len(result) > 0
    assert all(r.resolution == timedelta(hours=1) for r in result)
