"""Tests added by the B14 mutation pass for energidataservice and svk."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EnergiDataServiceClient,
    RateLimitError,
    ReserveProduct,
    SourceError,
    SvKClient,
)

START = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)
END = START + timedelta(minutes=30)

Handler = Callable[[httpx.Request], httpx.Response]

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


def eds_page(records: list[dict[str, Any]], total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"total": len(records) if total is None else total, "records": records}
    )


def eds_client(handler: Handler, **kwargs: Any) -> EnergiDataServiceClient:
    return EnergiDataServiceClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None, **kwargs
    )


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


def test_imbalance_rejects_empty_window() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        eds_client(lambda _: eds_page([])).imbalance_prices(START, START)


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


def test_eds_accepts_zero_retry_budget_and_fails_at_once() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"})

    with pytest.raises(RateLimitError):
        EnergiDataServiceClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
            sleep=lambda _: pytest.fail("no retries means no sleeps"),
        ).imbalance_prices(START, END)
    assert len(calls) == 1


# --- FCR (energidataservice) ---------------------------------------------------

FCR_START = datetime(2026, 10, 5, 22, tzinfo=UTC)

ND_TOTAL: dict[str, Any] = {
    "HourUTC": "2026-10-05T22:00:00",
    "HourDK": "2026-10-06T00:00:00",
    "PriceArea": "DK2",
    "ProductName": "FCR-N",
    "AuctionType": "Total",
    "PurchasedVolumeLocal": 8.4,
    "PurchasedVolumeTotal": 246.0,
    "PriceTotalEUR": 19.300813,
}


def eds_fcr(rows: list[dict[str, Any]], start: datetime, end: datetime) -> list[Any]:
    return eds_client(lambda _: eds_page(rows)).capacity_market(
        ReserveProduct.FCR_N, start, end, zones=(BiddingZone.DK2,)
    )


def test_eds_fcr_rejects_row_at_the_query_end() -> None:
    row = {**ND_TOTAL, "HourUTC": "2026-10-05T23:00:00"}
    with pytest.raises(SourceError, match="outside requested window"):
        eds_fcr([row], FCR_START, FCR_START + timedelta(hours=1))


def test_eds_fcr_expands_exactly_four_quarters() -> None:
    result = eds_fcr([ND_TOTAL], FCR_START, FCR_START + timedelta(hours=1, minutes=15))
    assert [r.start for r in result] == [FCR_START + q * INTERVAL for q in range(4)]


@pytest.mark.parametrize(
    ("domestic", "expected_sort"),
    [(True, "HourUTC asc"), (False, "HourUTC asc,PriceArea asc")],
)
def test_eds_fcr_sort_param_matches_the_dataset(domestic: bool, expected_sort: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return eds_page([])

    if domestic:
        zones: tuple[BiddingZone, ...] = (BiddingZone.DK1,)
        product = ReserveProduct.FCR
    else:
        zones = (BiddingZone.DK2,)
        product = ReserveProduct.FCR_N
    eds_client(handler).capacity_market(
        product, FCR_START, FCR_START + timedelta(hours=1), zones=zones
    )
    assert seen[0].url.params["sort"] == expected_sort


# --- SvK -----------------------------------------------------------------------

SVK_START = datetime(2026, 10, 8, tzinfo=UTC)
SVK_RECORD: dict[str, Any] = {
    "start_time_utc": "2026-10-08T00:00:00",
    "bidding_zone": "SE3",
    "reserve_product": "mFRRCapacityMarket",
    "reserve_direction": "up",
    "price": 7.2,
    "price_unit": "EUR-MW",
    "volume": 594.0,
    "volume_unit": "MW",
}


def svk_client(datastore: Handler, **kwargs: Any) -> SvKClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"resources": [{"id": "rid", "datastore_active": True}]},
                },
            )
        return datastore(request)

    return SvKClient(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None, **kwargs
    )


def svk_search(records: list[Any], total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "success": True,
            "result": {"total": len(records) if total is None else total, "records": records},
        },
    )


def test_svk_afrr_records_keep_their_product_and_reject_mfrr_names() -> None:
    row = {**SVK_RECORD, "reserve_product": "aFRRCapacityMarket"}
    result = svk_client(lambda _: svk_search([row])).capacity_market(
        ReserveProduct.AFRR, SVK_START, SVK_START + timedelta(hours=1), [BiddingZone.SE3]
    )
    assert len(result) == 4
    assert all(r.product is ReserveProduct.AFRR for r in result)
    with pytest.raises(SourceError, match="unexpected reserve_product"):
        svk_client(lambda _: svk_search([SVK_RECORD])).capacity_market(
            ReserveProduct.AFRR, SVK_START, SVK_START + timedelta(hours=1), [BiddingZone.SE3]
        )


def test_svk_floors_a_sub_second_start_before_chunking() -> None:
    result = svk_client(lambda _: svk_search([SVK_RECORD])).capacity_market(
        ReserveProduct.MFRR,
        SVK_START + timedelta(microseconds=1),
        SVK_START + timedelta(hours=1),
        [BiddingZone.SE3],
    )
    assert [r.start for r in result] == [SVK_START + q * INTERVAL for q in (1, 2, 3)]


def test_svk_rejects_boolean_total() -> None:
    body = {"success": True, "result": {"total": True, "records": []}}
    with pytest.raises(SourceError, match="invalid total"):
        svk_client(lambda _: httpx.Response(200, json=body)).capacity_market(
            ReserveProduct.MFRR, SVK_START, SVK_START + timedelta(hours=1), [BiddingZone.SE3]
        )


def test_svk_rejects_non_boolean_datastore_active() -> None:
    body = {"success": True, "result": {"resources": [{"id": "rid", "datastore_active": 1}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(200, json=body)
        pytest.fail("no datastore resource must not reach datastore_search")

    svk = SvKClient(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
    with pytest.raises(SourceError, match="no datastore resource"):
        svk.capacity_market(
            ReserveProduct.MFRR, SVK_START, SVK_START + timedelta(hours=1), [BiddingZone.SE3]
        )


def test_svk_rejects_truthy_non_boolean_success() -> None:
    body = {"success": 1, "result": {"total": 0, "records": []}}
    with pytest.raises(SourceError, match="reported failure"):
        svk_client(lambda _: httpx.Response(200, json=body)).capacity_market(
            ReserveProduct.MFRR, SVK_START, SVK_START + timedelta(hours=1), [BiddingZone.SE3]
        )


def test_svk_sorts_zones_before_directions_within_each_quarter() -> None:
    records = [
        {**SVK_RECORD, "bidding_zone": "SE4", "reserve_direction": "down"},
        {**SVK_RECORD, "bidding_zone": "SE3", "reserve_direction": "up"},
        {**SVK_RECORD, "bidding_zone": "SE3", "reserve_direction": "down"},
        {**SVK_RECORD, "bidding_zone": "SE4", "reserve_direction": "up"},
    ]
    result = svk_client(lambda _: svk_search(records)).capacity_market(
        ReserveProduct.MFRR,
        SVK_START,
        SVK_START + timedelta(hours=1),
        [BiddingZone.SE3, BiddingZone.SE4],
    )
    assert [(r.start, r.zone, r.direction) for r in result] == [
        (SVK_START + q * INTERVAL, zone, direction)
        for q in range(4)
        for zone in (BiddingZone.SE3, BiddingZone.SE4)
        for direction in (Direction.DOWN, Direction.UP)
    ]


def test_svk_accepts_zero_retry_budget_and_fails_at_once() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/package_show"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"resources": [{"id": "rid", "datastore_active": True}]},
                },
            )
        return httpx.Response(429, headers={"Retry-After": "1"})

    with pytest.raises(RateLimitError):
        SvKClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
            sleep=lambda _: pytest.fail("no retries means no sleeps"),
        ).capacity_market(
            ReserveProduct.MFRR, SVK_START, SVK_START + timedelta(hours=1), [BiddingZone.SE3]
        )
    assert len(calls) == 2
