"""The frozen fixture workloads, shared by record.py, run.py and compare_outputs.py.

Each workload's ``run(http, *, api_key, max_workers)`` performs the exact client
call named in ``call`` so the recording and the replayed measurement can never
drift apart. ``api_key=None`` makes clients fall back to their environment
variables (live recording); replay passes a dummy value.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from nordic_balancing import BiddingZone, ReserveProduct
from nordic_balancing.sources import EnergiDataServiceClient, EntsoeClient, ESettClient

_ALL_ZONES = tuple(BiddingZone)
_FCR_ND_ZONES = (
    BiddingZone.DK2,
    BiddingZone.SE1,
    BiddingZone.SE2,
    BiddingZone.SE3,
    BiddingZone.SE4,
)
_SE3 = (BiddingZone.SE3,)


def _d(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@dataclass(frozen=True)
class Workload:
    provider: str
    call: str
    min_interval_s: float  # pacing for live recording only
    run: Callable[..., list]  # (http, *, api_key=None, max_workers=None) -> list


def _entsoe_imbalance(start: datetime, end: datetime, zones: Any) -> Callable[..., list]:
    def run(http: httpx.Client, *, api_key=None, max_workers=None) -> list:
        kwargs = {} if max_workers is None else {"max_workers": max_workers}
        return EntsoeClient(api_key, http=http, **kwargs).imbalance_prices(start, end, zones=zones)

    return run


def _entsoe_capacity(product: ReserveProduct, start: datetime, end: datetime) -> Callable:
    def run(http: httpx.Client, *, api_key=None, max_workers=None) -> list:
        kwargs = {} if max_workers is None else {"max_workers": max_workers}
        return EntsoeClient(api_key, http=http, **kwargs).capacity_market(product, start, end)

    return run


def _esett_imbalance(start: datetime, end: datetime, zones: Any) -> Callable[..., list]:
    def run(http: httpx.Client, *, api_key=None, max_workers=None) -> list:
        return ESettClient(http=http).imbalance_prices(start, end, zones=zones)

    return run


def _eds_imbalance(start: datetime, end: datetime) -> Callable[..., list]:
    def run(http: httpx.Client, *, api_key=None, max_workers=None) -> list:
        return EnergiDataServiceClient(http=http).imbalance_prices(start, end)

    return run


def _eds_capacity(product: ReserveProduct, start: datetime, end: datetime, zones) -> Callable:
    def run(http: httpx.Client, *, api_key=None, max_workers=None) -> list:
        return EnergiDataServiceClient(http=http).capacity_market(product, start, end, zones=zones)

    return run


WORKLOADS: dict[str, Workload] = {
    "entsoe-imb-SE3-day-2025-10-26": Workload(
        "entsoe",
        "EntsoeClient.imbalance_prices(datetime(2025,10,26,tzinfo=UTC), "
        "datetime(2025,10,27,tzinfo=UTC), zones=(BiddingZone.SE3,))",
        1.0,
        _entsoe_imbalance(_d(2025, 10, 26), _d(2025, 10, 27), _SE3),
    ),
    "entsoe-imb-SE3-month-2025-10": Workload(
        "entsoe",
        "EntsoeClient.imbalance_prices(datetime(2025,10,1,tzinfo=UTC), "
        "datetime(2025,11,1,tzinfo=UTC), zones=(BiddingZone.SE3,))",
        1.0,
        _entsoe_imbalance(_d(2025, 10, 1), _d(2025, 11, 1), _SE3),
    ),
    "entsoe-imb-SE3-year-2025": Workload(
        "entsoe",
        "EntsoeClient.imbalance_prices(datetime(2025,1,1,tzinfo=UTC), "
        "datetime(2026,1,1,tzinfo=UTC), zones=(BiddingZone.SE3,))",
        1.0,
        _entsoe_imbalance(_d(2025, 1, 1), _d(2026, 1, 1), _SE3),
    ),
    "entsoe-imb-all-year-2025": Workload(
        "entsoe",
        "EntsoeClient.imbalance_prices(datetime(2025,1,1,tzinfo=UTC), "
        "datetime(2026,1,1,tzinfo=UTC), zones=tuple(BiddingZone))",
        1.0,
        _entsoe_imbalance(_d(2025, 1, 1), _d(2026, 1, 1), _ALL_ZONES),
    ),
    "entsoe-imb-SE3-year-2022": Workload(
        "entsoe",
        "EntsoeClient.imbalance_prices(datetime(2022,1,1,tzinfo=UTC), "
        "datetime(2023,1,1,tzinfo=UTC), zones=(BiddingZone.SE3,))",
        1.0,
        _entsoe_imbalance(_d(2022, 1, 1), _d(2023, 1, 1), _SE3),
    ),
    "entsoe-cap-FCRN-NO-year-2025": Workload(
        "entsoe",
        "EntsoeClient.capacity_market(ReserveProduct.FCR_N, "
        "datetime(2025,1,1,tzinfo=UTC), datetime(2026,1,1,tzinfo=UTC))",
        1.0,
        _entsoe_capacity(ReserveProduct.FCR_N, _d(2025, 1, 1), _d(2026, 1, 1)),
    ),
    "esett-imb-SE3-year-2025": Workload(
        "esett",
        "ESettClient.imbalance_prices(datetime(2025,1,1,tzinfo=UTC), "
        "datetime(2026,1,1,tzinfo=UTC), zones=(BiddingZone.SE3,))",
        1.0,
        _esett_imbalance(_d(2025, 1, 1), _d(2026, 1, 1), _SE3),
    ),
    "esett-imb-all-year-2025": Workload(
        "esett",
        "ESettClient.imbalance_prices(datetime(2025,1,1,tzinfo=UTC), "
        "datetime(2026,1,1,tzinfo=UTC), zones=tuple(BiddingZone))",
        1.0,
        _esett_imbalance(_d(2025, 1, 1), _d(2026, 1, 1), _ALL_ZONES),
    ),
    "eds-imb-DK-year-2025": Workload(
        "energidataservice",
        "EnergiDataServiceClient.imbalance_prices(datetime(2025,1,1,tzinfo=UTC), "
        "datetime(2026,1,1,tzinfo=UTC), zones=(BiddingZone.DK1, BiddingZone.DK2))",
        # EDS rate-limits per dataset; 3 s pacing still produced a 429 mid-year.
        8.0,
        _eds_imbalance(_d(2025, 1, 1), _d(2026, 1, 1)),
    ),
    "eds-cap-FCRN-year-2025": Workload(
        "energidataservice",
        "EnergiDataServiceClient.capacity_market(ReserveProduct.FCR_N, "
        "datetime(2025,1,1,tzinfo=UTC), datetime(2026,1,1,tzinfo=UTC), "
        "zones=(BiddingZone.DK2, BiddingZone.SE1, BiddingZone.SE2, BiddingZone.SE3, "
        "BiddingZone.SE4))",
        # FcrNdDK2's Retry-After is ~113 s; first responses must stay clean.
        130.0,
        _eds_capacity(ReserveProduct.FCR_N, _d(2025, 1, 1), _d(2026, 1, 1), _FCR_ND_ZONES),
    ),
    "eds-cap-aFRR-month-2025-10": Workload(
        "energidataservice",
        "EnergiDataServiceClient.capacity_market(ReserveProduct.AFRR, "
        "datetime(2025,10,1,tzinfo=UTC), datetime(2025,11,1,tzinfo=UTC), "
        "zones=tuple(BiddingZone))",
        8.0,
        _eds_capacity(ReserveProduct.AFRR, _d(2025, 10, 1), _d(2025, 11, 1), _ALL_ZONES),
    ),
}
