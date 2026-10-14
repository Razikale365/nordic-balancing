import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    EnergiDataServiceClient,
    EntsoeClient,
    ESettClient,
    FingridClient,
    ImbalancePrice,
    ReserveProduct,
    SvKClient,
)
from nordic_balancing.sources.svk import _DATASETS, BASE_URL


def _recent_day() -> tuple[datetime, datetime]:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=8)
    return start, start + timedelta(days=1)


def _on_grid_in_window(record_start: datetime, start: datetime, end: datetime) -> bool:
    return (
        start <= record_start < end
        and record_start.tzinfo is not None
        and record_start.utcoffset() == timedelta(0)
        and (record_start - start) % INTERVAL == timedelta(0)
    )


def _assert_recent_imbalance_prices(
    prices: list[ImbalancePrice],
    start: datetime,
    end: datetime,
    zone: BiddingZone,
) -> None:
    assert len(prices) > 0
    assert all(_on_grid_in_window(p.start, start, end) for p in prices)
    assert all(p.zone is zone for p in prices)
    assert all(p.resolution == INTERVAL for p in prices)
    assert any(p.imbalance_price_eur is not None for p in prices)


@pytest.mark.live
def test_live_recent_energidataservice_dk1() -> None:
    start, end = _recent_day()
    with EnergiDataServiceClient() as eds:
        prices = eds.imbalance_prices(start, end, [BiddingZone.DK1])
    _assert_recent_imbalance_prices(prices, start, end, BiddingZone.DK1)


@pytest.mark.live
def test_live_recent_esett_se3() -> None:
    start, end = _recent_day()
    with ESettClient() as esett:
        prices = esett.imbalance_prices(start, end, [BiddingZone.SE3])
    _assert_recent_imbalance_prices(prices, start, end, BiddingZone.SE3)


@pytest.mark.live
def test_live_recent_fingrid_fi() -> None:
    if not os.environ.get("FINGRID_API_KEY"):
        pytest.skip("FINGRID_API_KEY is not set and no key was found in the repo .env")
    start, end = _recent_day()
    with FingridClient() as fingrid:
        prices = fingrid.imbalance_prices(start, end)
    _assert_recent_imbalance_prices(prices, start, end, BiddingZone.FI)


@pytest.mark.live
def test_live_recent_entsoe_se3() -> None:
    if not os.environ.get("ENTSOE_API_KEY"):
        pytest.skip("ENTSOE_API_KEY not set")
    start, end = _recent_day()
    with EntsoeClient() as entsoe:
        prices = entsoe.imbalance_prices(start, end, [BiddingZone.SE3])
    _assert_recent_imbalance_prices(prices, start, end, BiddingZone.SE3)


@pytest.mark.live
def test_live_recent_svk() -> None:
    start, end = _recent_day()
    with SvKClient() as svk:
        records = svk.capacity_market(ReserveProduct.MFRR, start, end)
    assert len(records) > 0
    assert all(_on_grid_in_window(r.start, start, end) for r in records)


@pytest.mark.live
def test_svk_may_have_moved_to_15_minute_records() -> None:
    dataset = _DATASETS[ReserveProduct.MFRR][0]
    with httpx.Client(timeout=30.0) as http:
        package_response = http.get(f"{BASE_URL}/package_show", params={"id": dataset})
        package_response.raise_for_status()
        package_body = package_response.json()
        assert isinstance(package_body, dict) and package_body.get("success") is True
        package_result = package_body.get("result")
        assert isinstance(package_result, dict)
        resources = package_result.get("resources")
        assert isinstance(resources, list)
        resource_id = next(
            (
                resource["id"]
                for resource in resources
                if isinstance(resource, dict)
                and resource.get("datastore_active") is True
                and isinstance(resource.get("id"), str)
            ),
            None,
        )
        assert resource_id is not None, f"no datastore resource for {dataset}"

        search_response = http.get(
            f"{BASE_URL}/datastore_search",
            params={
                "resource_id": resource_id,
                "sort": "start_time_utc desc",
                "limit": "200",
            },
        )
        search_response.raise_for_status()
        search_body = search_response.json()
    assert isinstance(search_body, dict) and search_body.get("success") is True
    search_result = search_body.get("result")
    assert isinstance(search_result, dict)
    records = search_result.get("records")
    assert isinstance(records, list) and len(records) > 0
    non_hourly: list[str] = []
    for record in records:
        assert isinstance(record, dict)
        raw_start = record.get("start_time_utc")
        assert isinstance(raw_start, str)
        parsed = datetime.fromisoformat(raw_start)
        if parsed.minute != 0 or parsed.second != 0 or parsed.microsecond != 0:
            non_hourly.append(raw_start)
    assert not non_hourly, (
        f"SvK may have moved to 15-minute records: non-hourly start_time_utc {non_hourly[:5]!r}"
    )
