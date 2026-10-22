"""An hourly record expands to quarters, and none starts at or after the query ``end``."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fakes import (
    EDS_CAPACITY_ROW,
    eds_client,
    eds_page,
    entsoe_client,
    entsoe_document,
    entsoe_period,
    entsoe_point,
    entsoe_series,
    esett_client,
    esett_server,
    fingrid_client,
    fingrid_server,
    http_client,
    svk_server,
)

from nordic_balancing import INTERVAL, BiddingZone, ReserveProduct, SvKClient

HOURLY_ERA = datetime(2023, 1, 10, 10, tzinfo=UTC)
FINGRID_HOURLY_ERA = datetime(2024, 6, 10, 10, tzinfo=UTC)
EDS_ROW_HOUR = datetime(2026, 10, 9, 2, tzinfo=UTC)
ENTSOE_HOUR = datetime(2026, 1, 15, tzinfo=UTC)


def esett(start: datetime, end: datetime) -> list[datetime]:
    prices = esett_client(esett_server).imbalance_prices(start, end, [BiddingZone.SE3])
    return [p.start for p in prices]


def svk(start: datetime, end: datetime) -> list[datetime]:
    with SvKClient(http_client(svk_server), sleep=lambda _: None) as client:
        rows = client.capacity_market(ReserveProduct.MFRR, start, end, [BiddingZone.SE3])
    return sorted({r.start for r in rows})


def fingrid(start: datetime, end: datetime) -> list[datetime]:
    rows = fingrid_client(fingrid_server).capacity_market(ReserveProduct.FCR_N, start, end)
    return sorted({r.start for r in rows})


def eds(start: datetime, end: datetime) -> list[datetime]:
    client = eds_client(lambda _: eds_page([EDS_CAPACITY_ROW]))
    rows = client.capacity_market(ReserveProduct.AFRR, start, end, zones=[BiddingZone.DK1])
    return sorted({r.start for r in rows})


def entsoe(start: datetime, end: datetime) -> list[datetime]:
    document = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z", "2026-01-15T01:00Z", entsoe_point(1), resolution="PT60M"
            )
        )
    )
    client = entsoe_client(lambda _: httpx.Response(200, text=document))
    return [p.start for p in client.imbalance_prices(start, end, [BiddingZone.SE3])]


CASES: dict[str, tuple[Callable[[datetime, datetime], list[datetime]], datetime]] = {
    "esett": (esett, HOURLY_ERA),
    "svk": (svk, HOURLY_ERA),
    "fingrid": (fingrid, FINGRID_HOURLY_ERA),
    "eds": (eds, EDS_ROW_HOUR),
    "entsoe": (entsoe, ENTSOE_HOUR),
}


@pytest.mark.parametrize("source", list(CASES))
# 15 and 45 put a quarter of the hour exactly at `end`; 60 pins the full four.
@pytest.mark.parametrize("minutes", [15, 45, 60])
def test_hourly_expansion_stops_before_the_window_end(source: str, minutes: int) -> None:
    fetch, hour = CASES[source]
    starts = fetch(hour, hour + timedelta(minutes=minutes))
    assert starts == [hour + i * INTERVAL for i in range(minutes // 15)]
