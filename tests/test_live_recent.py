import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EnergiDataServiceClient,
    EntsoeClient,
    ESettClient,
    FingridClient,
    ImbalancePrice,
    ReserveCapacity,
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
def test_live_recent_energidataservice_afrr_capacity() -> None:
    start, end = _recent_day()
    with EnergiDataServiceClient() as eds:
        records = eds.capacity_market(ReserveProduct.AFRR, start, end, zones=list(BiddingZone))
    expected = {
        (start + quarter * INTERVAL, zone, direction)
        for quarter in range(96)
        for zone in BiddingZone
        for direction in (Direction.UP, Direction.DOWN)
    }
    assert len(records) == len(expected)
    assert {(r.start, r.zone, r.direction) for r in records} == expected
    assert all(r.product is ReserveProduct.AFRR for r in records)
    assert all(r.resolution == timedelta(hours=1) for r in records)
    assert all(r.source == "energidataservice" for r in records)
    assert all(r.price_eur_per_mw is not None and r.volume_mw is not None for r in records)
    assert all(_on_grid_in_window(r.start, start, end) for r in records)


@pytest.mark.live
def test_live_recent_energidataservice_mfrr_capacity() -> None:
    start, end = _recent_day()
    with EnergiDataServiceClient() as eds:
        records = eds.capacity_market(ReserveProduct.MFRR, start, end)
    expected = {
        (start + quarter * INTERVAL, zone, direction)
        for quarter in range(96)
        for zone in (BiddingZone.DK1, BiddingZone.DK2)
        for direction in (Direction.UP, Direction.DOWN)
    }
    assert len(records) == len(expected)
    assert {(r.start, r.zone, r.direction) for r in records} == expected
    assert all(r.product is ReserveProduct.MFRR for r in records)
    assert all(r.resolution == timedelta(hours=1) for r in records)
    assert all(r.source == "energidataservice" for r in records)
    assert all(r.price_eur_per_mw is not None and r.volume_mw is not None for r in records)
    assert all(_on_grid_in_window(r.start, start, end) for r in records)


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


@pytest.mark.live
@pytest.mark.parametrize(
    ("product", "zone", "directions"),
    [
        (ReserveProduct.FCR_N, BiddingZone.SE3, {Direction.NONE}),
        (ReserveProduct.FCR_D, BiddingZone.DK2, {Direction.UP, Direction.DOWN}),
        (ReserveProduct.FCR, BiddingZone.DK1, {Direction.NONE}),
    ],
)
def test_live_recent_eds_fcr_capacity(
    product: ReserveProduct,
    zone: BiddingZone,
    directions: set[Direction],
) -> None:
    start, _ = _recent_day()
    end = start + timedelta(days=2)
    with EnergiDataServiceClient() as eds:
        records = eds.capacity_market(product, start, end, zones=[zone])
    assert records
    assert {r.direction for r in records} == directions
    assert all(r.product is product and r.zone is zone for r in records)
    assert all(
        r.source == "energidataservice" and r.resolution == timedelta(hours=1) for r in records
    )
    assert all(_on_grid_in_window(r.start, start, end) for r in records)
    assert all(r.price_eur_per_mw is not None and r.volume_mw is not None for r in records)
    if product is ReserveProduct.FCR:
        assert all(
            r.volume_mw == r.raw["FCRdomestic_MW"] and r.price_eur_per_mw == r.raw["FCRdk_EUR"]
            for r in records
        )
    else:
        assert all(
            r.raw["AuctionType"] == "Total"
            and r.volume_mw == r.raw["PurchasedVolumeLocal"]
            and r.price_eur_per_mw == r.raw["PriceTotalEUR"]
            for r in records
        )


@pytest.mark.live
def test_live_recent_entsoe_no1_fcr_n_capacity() -> None:
    if not os.environ.get("ENTSOE_API_KEY"):
        pytest.skip("ENTSOE_API_KEY not set")
    start, _ = _recent_day()
    end = start + timedelta(days=2)
    with EntsoeClient() as client:
        records = client.capacity_market(ReserveProduct.FCR_N, start, end, zones=[BiddingZone.NO1])
    _assert_recent_fi_no_capacity(
        records, start, end, ReserveProduct.FCR_N, BiddingZone.NO1, "entsoe"
    )
    assert all(
        r.raw["currency_Unit.name"] == "EUR" and r.raw["quantity_Measure_Unit.name"] == "MAW"
        for r in records
    )


def _assert_recent_fi_no_capacity(
    records: list[ReserveCapacity],
    start: datetime,
    end: datetime,
    product: ReserveProduct,
    zone: BiddingZone,
    source: str,
) -> None:
    directions = (
        {Direction.NONE} if product is ReserveProduct.FCR_N else {Direction.UP, Direction.DOWN}
    )
    expected = {
        (start + q * INTERVAL, d) for q in range((end - start) // INTERVAL) for d in directions
    }
    if source == "entsoe":
        assert len(expected) * 95 // 100 <= len(records) <= len(expected), (
            f"{product.value} {zone.value} recent capacity: got {len(records)} records; "
            f"expected {len(expected)}, require at least 95% ({len(expected) * 95 // 100})"
        )
        seen = {(r.start, r.direction) for r in records}
        assert len(seen) == len(records) and seen <= expected
    else:
        assert len(records) == len(expected)
        assert {(r.start, r.direction) for r in records} == expected
        assert all(r.price_eur_per_mw is not None and r.volume_mw is not None for r in records)
    assert all(r.zone is zone and r.product is product and r.source == source for r in records)
    assert all(r.resolution == timedelta(hours=1) for r in records)
    assert all(_on_grid_in_window(r.start, start, end) for r in records)


@pytest.mark.live
@pytest.mark.parametrize(
    "product", [ReserveProduct.FCR_N, ReserveProduct.FCR_D, ReserveProduct.MFRR]
)
def test_live_recent_fingrid_capacity(product: ReserveProduct) -> None:
    if not os.environ.get("FINGRID_API_KEY"):
        pytest.skip("FINGRID_API_KEY not set")
    start, end = _recent_day()
    with FingridClient() as client:
        records = client.capacity_market(product, start, end)
    _assert_recent_fi_no_capacity(records, start, end, product, BiddingZone.FI, "fingrid")


@pytest.mark.live
@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_live_recent_entsoe_mfrr_capacity(zone: BiddingZone) -> None:
    if not os.environ.get("ENTSOE_API_KEY"):
        pytest.skip("ENTSOE_API_KEY not set")
    start, end = _recent_day()
    with EntsoeClient() as client:
        records = client.capacity_market(ReserveProduct.MFRR, start, end, zones=[zone])
    _assert_recent_fi_no_capacity(records, start, end, ReserveProduct.MFRR, zone, "entsoe")


@pytest.mark.live
def test_live_recent_fi_mfrr_entsoe_equals_fingrid() -> None:
    if not all(os.environ.get(key) for key in ("FINGRID_API_KEY", "ENTSOE_API_KEY")):
        pytest.skip("FINGRID_API_KEY and ENTSOE_API_KEY required")
    start, end = _recent_day()
    with FingridClient() as fg, EntsoeClient() as en:
        fingrid_records = fg.capacity_market(ReserveProduct.MFRR, start, end)
        entsoe_records = en.capacity_market(ReserveProduct.MFRR, start, end, zones=[BiddingZone.FI])
    for records, source in ((fingrid_records, "fingrid"), (entsoe_records, "entsoe")):
        _assert_recent_fi_no_capacity(
            records, start, end, ReserveProduct.MFRR, BiddingZone.FI, source
        )
    assert {(r.start, r.direction): (r.price_eur_per_mw, r.volume_mw) for r in fingrid_records} == {
        (r.start, r.direction): (r.price_eur_per_mw, r.volume_mw) for r in entsoe_records
    }


@pytest.mark.live
@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.MFRR])
def test_live_entsoe_no1_capacity_sixty_days(product: ReserveProduct) -> None:
    if not os.environ.get("ENTSOE_API_KEY"):
        pytest.skip("ENTSOE_API_KEY not set")
    end, _ = _recent_day()
    start = end - timedelta(days=60)
    with EntsoeClient() as client:
        records = client.capacity_market(product, start, end, zones=[BiddingZone.NO1])
    directions = (
        {Direction.NONE} if product is ReserveProduct.FCR_N else {Direction.UP, Direction.DOWN}
    )
    expected = 60 * 24 * 4 * len(directions)
    assert expected * 95 // 100 <= len(records) <= expected, (
        f"{product.value} NO1 60-day capacity: got {len(records)} records; "
        f"expected {expected}, require at least 95% ({expected * 95 // 100})"
    )
    assert len({(r.start, r.direction) for r in records}) == len(records)
    assert all(r.zone is BiddingZone.NO1 and r.product is product for r in records)
    assert all(r.direction in directions and r.resolution == timedelta(hours=1) for r in records)
    assert all(r.source == "entsoe" and _on_grid_in_window(r.start, start, end) for r in records)


def _synthetic_mfrr_capacity(
    start: datetime,
    source: str,
    *,
    skip: set[int] | None = None,
    null_price: set[int] | None = None,
) -> list[ReserveCapacity]:
    return [
        ReserveCapacity(
            start=start + q * INTERVAL,
            zone=BiddingZone.NO1 if source == "entsoe" else BiddingZone.FI,
            product=ReserveProduct.MFRR,
            direction=direction,
            price_eur_per_mw=None if null_price and q in null_price else 12.5,
            volume_mw=50.0,
            source=source,
            resolution=timedelta(hours=1),
            raw={},
        )
        for q in range(96)
        if not (skip and q in skip)
        for direction in (Direction.UP, Direction.DOWN)
    ]


def test_recent_fi_no_capacity_tolerates_entsoe_gaps_and_nulls() -> None:
    start, end = _recent_day()
    full = _synthetic_mfrr_capacity(start, "entsoe")
    for records in (
        full,
        _synthetic_mfrr_capacity(start, "entsoe", skip={40}),
        _synthetic_mfrr_capacity(start, "entsoe", null_price={10, 11}),
    ):
        _assert_recent_fi_no_capacity(
            records, start, end, ReserveProduct.MFRR, BiddingZone.NO1, "entsoe"
        )
    for records in (
        _synthetic_mfrr_capacity(start, "entsoe", skip=set(range(10))),
        [*full[:-1], full[0]],
    ):
        with pytest.raises(AssertionError):
            _assert_recent_fi_no_capacity(
                records, start, end, ReserveProduct.MFRR, BiddingZone.NO1, "entsoe"
            )


def test_recent_fi_no_capacity_stays_strict_for_fingrid() -> None:
    start, end = _recent_day()
    _assert_recent_fi_no_capacity(
        _synthetic_mfrr_capacity(start, "fingrid"),
        start,
        end,
        ReserveProduct.MFRR,
        BiddingZone.FI,
        "fingrid",
    )
    for records in (
        _synthetic_mfrr_capacity(start, "fingrid", skip={40}),
        _synthetic_mfrr_capacity(start, "fingrid", null_price={10}),
    ):
        with pytest.raises(AssertionError):
            _assert_recent_fi_no_capacity(
                records, start, end, ReserveProduct.MFRR, BiddingZone.FI, "fingrid"
            )
