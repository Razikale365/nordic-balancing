"""Fake data sources shared by the tests.

Every test talks to the clients through an ``httpx.MockTransport``; this module
holds the pieces that used to be rebuilt in each test file: the transport
wrapper, per-source response builders, and a few deterministic fake servers
that answer exactly the request window they are asked for.

It is a plain module (not a test file). Tests import it as ``from fakes import ...``.
"""

import io
import json
import zipfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    EnergiDataServiceClient,
    EntsoeClient,
    ESettClient,
    FingridClient,
    ReserveCapacity,
    ReserveProduct,
    SvKClient,
)
from nordic_balancing.sources.entsoe import _EIC_CODES as _ENTSOE_EIC
from nordic_balancing.sources.esett import _EIC_CODES as _ESETT_EIC

Handler = Callable[[httpx.Request], httpx.Response]

HOUR = timedelta(hours=1)

# API keys the fake servers accept; the clients only need some non-empty string.
FINGRID_KEY = "test-fingrid-secret"
ENTSOE_KEY = "test-entsoe-token"

# Source switch-over times used by the deterministic servers below.
ESETT_SWITCH = datetime(2023, 5, 21, 22, tzinfo=UTC)
FINGRID_SWITCH = datetime(2025, 3, 18, 23, tzinfo=UTC)
FINGRID_QUARTER_ERA = datetime(2025, 3, 14, 23, 15, tzinfo=UTC)

_ESETT_ZONE = {code: zone for zone, code in _ESETT_EIC.items()}
_ENTSOE_ZONE = {code: zone for zone, code in _ENTSOE_EIC.items()}
_ZONES = tuple(BiddingZone)


# --- transport and time helpers ------------------------------------------------


def http_client(handler: Handler) -> httpx.Client:
    """An httpx client whose every request is answered by ``handler``."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def iso_z(stamp: datetime) -> str:
    return stamp.isoformat().replace("+00:00", "Z")


def ceil_quarter(stamp: datetime) -> datetime:
    floor = stamp.replace(minute=stamp.minute // 15 * 15, second=0, microsecond=0)
    return floor if floor == stamp else floor + INTERVAL


def floor_hour(stamp: datetime) -> datetime:
    return stamp.replace(minute=0, second=0, microsecond=0)


def ceil_hour(stamp: datetime) -> datetime:
    floor = floor_hour(stamp)
    return floor if floor == stamp else floor + HOUR


def quarter_grid(start: datetime, end: datetime) -> list[datetime]:
    """Every quarter-hour boundary in ``[start, end)``."""
    stamp = ceil_quarter(start)
    stamps: list[datetime] = []
    while stamp < end:
        stamps.append(stamp)
        stamp += INTERVAL
    return stamps


def zone_index(zone: BiddingZone) -> int:
    return _ZONES.index(zone)


def quarter_value(zone: BiddingZone, stamp: datetime) -> float:
    """The imbalance price the deterministic eSett, EDS and ENTSO-E servers publish."""
    return float(int(stamp.timestamp()) // 900 % 997) + zone_index(zone) / 100


# --- eSett ---------------------------------------------------------------------


def esett_row(
    stamp: datetime,
    zone: BiddingZone,
    *,
    sales: float | None,
    purchase: float | None,
    up: float | None = None,
    down: float | None = None,
    main_direction: float | None = None,
) -> dict[str, Any]:
    return {
        "timestampUTC": iso_z(stamp),
        "mba": zone.value,
        "imblSalesPrice": sales,
        "imblPurchasePrice": purchase,
        "upRegPrice": up,
        "downRegPrice": down,
        "mainDirRegPowerPerMBA": main_direction,
    }


def esett_client(handler: Handler, sleeps: list[float] | None = None) -> ESettClient:
    """An eSett client on a fake server; waits are appended to ``sleeps`` instead of slept."""
    recorded = sleeps if sleeps is not None else []
    return ESettClient(http_client(handler), sleep=recorded.append)


def esett_server(request: httpx.Request) -> httpx.Response:
    """Hourly rows before ``ESETT_SWITCH``, quarter-hour rows from then on."""
    params = request.url.params
    start = datetime.fromisoformat(params["start"])
    end = datetime.fromisoformat(params["end"])
    records = []
    for code in params.get_list("mba"):
        zone = _ESETT_ZONE[code]
        stamp = ceil_quarter(start)
        while stamp < end:
            if stamp >= ESETT_SWITCH or stamp.minute == 0:
                value = quarter_value(zone, stamp)
                records.append(esett_row(stamp, zone, sales=value, purchase=value))
            stamp += INTERVAL
    return httpx.Response(200, json=records)


# --- Energi Data Service -------------------------------------------------------


def eds_page(records: list[dict[str, Any]], total: int | None = None) -> httpx.Response:
    body = {"total": len(records) if total is None else total, "records": records}
    return httpx.Response(200, json=body)


def eds_client(
    handler: Handler, sleeps: list[float] | None = None, **kwargs: Any
) -> EnergiDataServiceClient:
    """An Energi Data Service client on a fake server; waits go to ``sleeps``."""
    recorded = sleeps if sleeps is not None else []
    return EnergiDataServiceClient(http_client(handler), sleep=recorded.append, **kwargs)


def eds_server(request: httpx.Request) -> httpx.Response:
    """Quarter-hour imbalance rows for the requested zones, paged by offset and limit."""
    params = request.url.params
    start = datetime.strptime(params["start"], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    end = datetime.strptime(params["end"], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    zones = sorted(BiddingZone[name] for name in json.loads(params["filter"])["PriceArea"])
    records = []
    for stamp in quarter_grid(start, end):
        for zone in zones:
            value = quarter_value(zone, stamp)
            records.append(
                {
                    "TimeUTC": stamp.strftime("%Y-%m-%dT%H:%M:%S"),
                    "PriceArea": zone.value,
                    "ImbalancePriceEUR": value,
                    "ImbalancePriceDKK": value * 7.45,
                    "SpotPriceEUR": value,
                    "DominatingDirection": int(stamp.timestamp()) // 900 % 3 - 1,
                    "SatisfiedDemand": 0.0,
                    "aFRRVWAUpEUR": 0.0,
                    "aFRRVWADownEUR": 0.0,
                    "mFRRMarginalPriceUpEUR": value,
                    "mFRRMarginalPriceDownEUR": value,
                }
            )
    offset = int(params["offset"])
    limit = int(params["limit"])
    records = records[offset : offset + limit]
    return httpx.Response(200, json={"total": len(records), "records": records})


# --- Fingrid -------------------------------------------------------------------


class FakeClock:
    """A clock and sleep pair for Fingrid's rate limiter: sleeping just moves time forward."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def fingrid_client(handler: Handler, timer: FakeClock | None = None) -> FingridClient:
    timer = timer if timer is not None else FakeClock()
    return FingridClient(FINGRID_KEY, http_client(handler), sleep=timer.sleep, clock=timer.clock)


def fingrid_page(
    records: list[Any], total: int | None = None, last_page: int = 1
) -> httpx.Response:
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


def fingrid_row(
    dataset: int, start: datetime, end: datetime, value: float | None
) -> dict[str, Any]:
    return {
        "datasetId": dataset,
        "startTime": iso_z(start),
        "endTime": iso_z(end),
        "value": value,
    }


def fingrid_value(dataset: int, stamp: datetime, hourly: bool) -> float:
    seed = int(stamp.timestamp()) // (3600 if hourly else 900) + dataset
    return float(seed % 997) + (1000.0 if hourly else 0.0)


def _fingrid_rows(dataset: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = [
        fingrid_row(dataset, stamp, stamp + INTERVAL, fingrid_value(dataset, stamp, False))
        for stamp in quarter_grid(start, end)
        if stamp >= FINGRID_QUARTER_ERA
    ]
    hour = floor_hour(start)
    while hour < end:
        if start <= hour < FINGRID_SWITCH:
            rows.append(fingrid_row(dataset, hour, hour + HOUR, fingrid_value(dataset, hour, True)))
        hour += HOUR
    rows.sort(key=lambda row: (row["startTime"], row["endTime"]))
    return rows


def fingrid_server(request: httpx.Request) -> httpx.Response:
    """Hourly rows until ``FINGRID_SWITCH``, overlapped by quarter-hour rows from March 2025."""
    dataset = int(request.url.path.split("/")[-2])
    params = request.url.params
    start = datetime.fromisoformat(params["startTime"])
    end = datetime.fromisoformat(params["endTime"])
    rows = _fingrid_rows(dataset, start, end)
    page_size = int(params["pageSize"])
    page = int(params["page"])
    total = len(rows)
    last_page = max(1, -(-total // page_size))
    return httpx.Response(
        200,
        json={
            "data": rows[(page - 1) * page_size : page * page_size],
            "pagination": {"total": total, "lastPage": last_page},
        },
    )


def fingrid_expected_value(stamp: datetime) -> float:
    """What dataset 319 resolves to at ``stamp`` when served by ``fingrid_server``."""
    if stamp < FINGRID_SWITCH:
        return fingrid_value(319, floor_hour(stamp), hourly=True)
    return fingrid_value(319, stamp, hourly=False)


# --- SvK -----------------------------------------------------------------------


SVK_RESOURCE_ID = "00000000-0000-0000-0000-000000000001"


def svk_package_show(resource_id: str = SVK_RESOURCE_ID) -> dict[str, Any]:
    return {
        "success": True,
        "result": {"resources": [{"id": resource_id, "datastore_active": True}]},
    }


def svk_search(records: list[Any], total: int | None = None) -> dict[str, Any]:
    return {
        "success": True,
        "result": {"total": len(records) if total is None else total, "records": records},
    }


def svk_page(records: list[Any], total: int | None = None) -> httpx.Response:
    return httpx.Response(200, json=svk_search(records, total))


def svk_client(
    datastore: Handler,
    sleeps: list[float] | None = None,
    resource_id: str = SVK_RESOURCE_ID,
    **kwargs: Any,
) -> SvKClient:
    """An SvK client whose package_show lookup is answered; ``datastore`` serves the rest."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(200, json=svk_package_show(resource_id))
        return datastore(request)

    recorded = sleeps if sleeps is not None else []
    return SvKClient(http_client(handler), sleep=recorded.append, **kwargs)


def svk_row(
    row_id: int,
    start: datetime,
    zone: str,
    direction: str,
    product: str,
    *,
    price: float | None,
    volume: float | None,
    swedish_offset: timedelta,
) -> dict[str, Any]:
    return {
        "_id": row_id,
        "start_time_sweden": (start + swedish_offset).strftime("%Y-%m-%dT%H:%M:%S"),
        "start_time_utc": start.strftime("%Y-%m-%dT%H:00:00"),
        "bidding_zone": zone,
        "reserve_product": product,
        "reserve_direction": direction,
        "price": price,
        "price_unit": "EUR-MW",
        "volume": volume,
        "volume_unit": "MW",
        "soda_hashbyte": f"{row_id:032x}",
        "soda_identity": row_id,
    }


def svk_price(zone: BiddingZone, hour: datetime, direction: str) -> float:
    seed = int(hour.timestamp()) // 3600 % 991
    return float(seed) + zone_index(zone) / 10 + (0.5 if direction == "up" else 0.0)


def svk_server(request: httpx.Request) -> httpx.Response:
    """One up and one down row per requested hour and zone; the dataset holds hourly rows only."""
    params = request.url.params
    if request.url.path.endswith("/package_show"):
        return httpx.Response(200, json=svk_package_show(f"rid-{params['id']}"))
    filters = json.loads(params["filters"])
    product = "mFRRCapacityMarket" if "mfrr" in params["resource_id"] else "aFRRCapacityMarket"
    records = []
    # The quarter-hour values in the filter match nothing.
    hours = sorted(h for h in filters["start_time_utc"] if h[14:16] == "00")
    for index, (hour, zone, direction) in enumerate(
        (hour, zone, direction)
        for hour in hours
        for zone in sorted(filters["bidding_zone"])
        for direction in ("down", "up")
    ):
        start = datetime.strptime(hour, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        records.append(
            svk_row(
                index,
                start,
                zone,
                direction,
                product,
                price=svk_price(BiddingZone[zone], start, direction),
                volume=float(int(start.timestamp()) // 3600 % 500),
                swedish_offset=timedelta(hours=2),
            )
        )
    offset = int(params["offset"])
    limit = int(params["limit"])
    return httpx.Response(200, json=svk_search(records[offset : offset + limit], len(records)))


# --- ENTSO-E -------------------------------------------------------------------

NS = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:0"
NS_45 = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:5"
ACK_NS = "urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0"


def entsoe_point(position: Any, amount: Any = 21.0, category: str | None = None) -> str:
    cat = f"<imbalance_Price.category>{category}</imbalance_Price.category>" if category else ""
    return (
        f"<Point><position>{position}</position>"
        f"<imbalance_Price.amount>{amount}</imbalance_Price.amount>{cat}</Point>"
    )


def entsoe_period(
    start: str,
    end: str,
    *points: str,
    resolution: str = "PT15M",
    interval: str | None = None,
) -> str:
    time_interval = (
        interval
        if interval is not None
        else f"<timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
    )
    return f"<Period>{time_interval}<resolution>{resolution}</resolution>{''.join(points)}</Period>"


def entsoe_series(
    *periods: str,
    curve: str | None = "A03",
    currency: str | None = "EUR",
) -> str:
    unit = f"<currency_Unit.name>{currency}</currency_Unit.name>" if currency is not None else ""
    tag = f"<curveType>{curve}</curveType>" if curve is not None else ""
    return f"<TimeSeries>{unit}{tag}{''.join(periods)}</TimeSeries>"


def entsoe_document(*series_: str, namespace: str = NS, head: str = "") -> str:
    return (
        f'<Balancing_MarketDocument xmlns="{namespace}"><type>A85</type>{head}{"".join(series_)}'
        "</Balancing_MarketDocument>"
    )


def entsoe_client(
    handler: Handler, sleeps: list[float] | None = None, **kwargs: Any
) -> EntsoeClient:
    recorded = sleeps if sleeps is not None else []
    return EntsoeClient(ENTSOE_KEY, http_client(handler), sleep=recorded.append, **kwargs)


def entsoe_acknowledgement(text: str, *additional_texts: str) -> str:
    texts = "".join(f"<text>{value}</text>" for value in (text, *additional_texts))
    return (
        f'<Acknowledgement_MarketDocument xmlns="{ACK_NS}">'
        f"<Reason><code>999</code>{texts}</Reason>"
        "</Acknowledgement_MarketDocument>"
    )


def zipped(*members: str | bytes, name: str = "member0.xml") -> bytes:
    """A ZIP archive of the members; the first is called ``name``, later ones ``member<n>.xml``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, member in enumerate(members):
            filename = name if index == 0 else f"member{index}.xml"
            # A fixed timestamp keeps the bytes, and parametrized test IDs, stable.
            info = zipfile.ZipInfo(filename, date_time=(2026, 1, 1, 0, 0, 0))
            archive.writestr(info, member, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def area_head(eic: str) -> str:
    return f'<area_Domain.mRID codingScheme="A01">{eic}</area_Domain.mRID>'


def entsoe_server(request: httpx.Request) -> httpx.Response:
    """One quarter-hour Period covering exactly the requested window."""
    params = request.url.params
    start = datetime.strptime(params["periodStart"], "%Y%m%d%H%M").replace(tzinfo=UTC)
    end = datetime.strptime(params["periodEnd"], "%Y%m%d%H%M").replace(tzinfo=UTC)
    eic = params["controlArea_Domain"]
    zone = _ENTSOE_ZONE[eic]
    points: Iterable[str] = (
        entsoe_point(index, quarter_value(zone, stamp))
        for index, stamp in enumerate(quarter_grid(start, end), start=1)
    )
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                start.strftime("%Y-%m-%dT%H:%MZ"), end.strftime("%Y-%m-%dT%H:%MZ"), *points
            )
        ),
        namespace=NS_45,
        head=area_head(eic),
    )
    return httpx.Response(200, text=body)


# --- Recorded Finland and Norway capacity responses -----------------------------

FI_NO_FIXTURES = Path(__file__).parent / "fixtures" / "fi_no_capacity"
FI_NO_START = datetime(2026, 10, 7, 22, tzinfo=UTC)
FI_NO_END = FI_NO_START + 24 * HOUR


def fi_no_datasets() -> dict[int, dict[str, Any]]:
    return {
        int(path.name.split("-")[1]): json.loads(path.read_text())
        for path in FI_NO_FIXTURES.glob("fingrid-*.json")
    }


def fi_no_fingrid_fetch(
    product: ReserveProduct,
    bodies: dict[int, dict[str, Any]] | None = None,
    start: datetime = FI_NO_START,
    end: datetime = FI_NO_END,
    seen: list[httpx.Request] | None = None,
) -> list[ReserveCapacity]:
    bodies = fi_no_datasets() if bodies is None else bodies

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.headers["x-api-key"] == "test-key"
        dataset = int(request.url.path.split("/")[-2])
        return httpx.Response(200, json=bodies[dataset])

    with (
        http_client(handler) as http,
        FingridClient("test-key", http, min_interval=0) as client,
    ):
        return client.capacity_market(product, start, end)


def fi_no_archive(zone: BiddingZone = BiddingZone.NO1) -> bytes:
    return (FI_NO_FIXTURES / f"entsoe-a81-a47-{zone.value.lower()}-2026-10-08.zip").read_bytes()


def fi_no_xml_member(zone: BiddingZone = BiddingZone.NO1) -> bytes:
    with zipfile.ZipFile(io.BytesIO(fi_no_archive(zone))) as source:
        return source.read(source.namelist()[0])


def fi_no_entsoe_fetch(
    content: bytes | None = None,
    zone: BiddingZone = BiddingZone.NO1,
    start: datetime = FI_NO_START,
    end: datetime = FI_NO_END,
    seen: list[httpx.Request] | None = None,
    product: ReserveProduct = ReserveProduct.MFRR,
) -> list[ReserveCapacity]:
    content = fi_no_archive(zone) if content is None else content

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, content=content)

    with (
        http_client(handler) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        return client.capacity_market(product, start, end, zones=[zone])


def zip_members(*members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for index, member in enumerate(members):
            target.writestr(f"{index}.xml", member)
    return buffer.getvalue()


EDS_CAPACITY_ROW: dict[str, Any] = {
    "TimeUTC": "2026-10-09T02:00:00",
    "TimeDK": "2026-10-09T04:00:00",
    "PriceArea": "DK1",
    "UpDemandMW": 20.0,
    "UpProcuredMW": 30.0,
    "UpPriceEUR": -2.5,
    "UpPriceDKK": -18.65,
    "DownDemandMW": 10.0,
    "DownProcuredMW": 0.0,
    "DownPriceEUR": 5.0,
    "DownPriceDKK": 37.3,
}
