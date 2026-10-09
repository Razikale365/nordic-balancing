"""Keyless eSett single imbalance prices, preserving publication resolution."""

import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import httpx

from nordic_balancing._http import get_json
from nordic_balancing._validate import finite_number, reject_duplicate
from nordic_balancing.errors import SourceError
from nordic_balancing.models import INTERVAL, BiddingZone, Direction, ImbalancePrice, to_utc

SOURCE = "esett"
BASE_URL = "https://api.opendata.esett.com"
_HOURLY_END = datetime(2023, 5, 21, 22, tzinfo=UTC)
_HOUR = timedelta(hours=1)
_CHUNK = timedelta(days=31)
_EIC_CODES = {
    BiddingZone.DK1: "10YDK-1--------W",
    BiddingZone.DK2: "10YDK-2--------M",
    BiddingZone.FI: "10YFI_1________U",
    BiddingZone.NO1: "10YNO_1________2",
    BiddingZone.NO2: "10YNO_2________T",
    BiddingZone.NO3: "10YNO_3________J",
    BiddingZone.NO4: "10YNO_4________9",
    BiddingZone.NO5: "10Y1001A1001A48H",
    BiddingZone.SE1: "10Y1001A1001A44P",
    BiddingZone.SE2: "10Y1001A1001A45N",
    BiddingZone.SE3: "10Y1001A1001A46L",
    BiddingZone.SE4: "10Y1001A1001A47J",
}


class ESettClient:
    """Synchronous eSett client with bounded requests and no authentication.

    Pass your own ``httpx.Client`` to control proxies, timeouts or transports;
    otherwise the client creates (and closes) one itself.
    """

    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        base_url: str = BASE_URL,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self._owns_http = http is None
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_http:
            self._http.close()

    def imbalance_prices(
        self,
        start: datetime,
        end: datetime,
        zones: Iterable[BiddingZone] = tuple(BiddingZone),
    ) -> list[ImbalancePrice]:
        """Return intervals starting in ``[start, end)``, sorted by start and zone.

        Single-price data starts 2021-10-31T23:00Z. Before 2023-05-21T22:00Z,
        each hourly record is repeated across four quarters, with its original
        resolution retained. Chunking bounds the size of all-zone responses.
        """
        start_utc = to_utc(start, "start")
        end_utc = to_utc(end, "end")
        if end_utc <= start_utc:
            raise ValueError("end must be after start")
        requested = list(zones)
        if any(not isinstance(z, BiddingZone) for z in requested):
            raise ValueError("zones must contain only BiddingZone values")
        zone_list = sorted(set(requested))
        if not zone_list:
            raise ValueError("zones must not be empty")
        query_start = start_utc.replace(microsecond=start_utc.microsecond // 1000 * 1000)
        if query_start < _HOURLY_END:
            # Include the hourly record whose expanded quarters cover start.
            query_start = query_start.replace(minute=0, second=0, microsecond=0)
        query_end = end_utc
        if remainder := query_end.microsecond % 1000:
            # Millisecond precision must not exclude an interval just before end.
            query_end += timedelta(microseconds=1000 - remainder)
        prices: list[ImbalancePrice] = []
        seen: set[tuple[BiddingZone, datetime]] = set()
        while query_start < query_end:
            chunk_end = min(query_start + _CHUNK, query_end)
            params = [
                ("start", _format(query_start)),
                ("end", _format(chunk_end)),
                *(("mba", _EIC_CODES[z]) for z in zone_list),
            ]
            try:
                body = get_json(
                    self._http,
                    self._base_url,
                    "/EXP14/Prices",
                    params,
                    source=SOURCE,
                    max_retries=self._max_retries,
                    sleep=self._sleep,
                )
            except httpx.HTTPError:
                raise SourceError(f"{SOURCE}: request failed for /EXP14/Prices") from None
            if not isinstance(body, list):
                raise SourceError(f"{SOURCE}: response for /EXP14/Prices is not a JSON array")
            for record in body:
                price = _parse_imbalance(record)
                if price.zone not in zone_list:
                    raise SourceError(f"{SOURCE}: unrequested mba {price.zone.value!r}")
                if not query_start <= price.start < chunk_end:
                    raise SourceError(f"{SOURCE}: timestampUTC outside requested chunk")
                reject_duplicate(
                    seen,
                    (price.zone, price.start),
                    source=SOURCE,
                    detail=f"record for {price.zone.value} at {price.start.isoformat()}",
                )
                quarters = 4 if price.resolution == _HOUR else 1
                for quarter in range(quarters):
                    interval_start = price.start + quarter * INTERVAL
                    if start_utc <= interval_start < end_utc:
                        prices.append(replace(price, start=interval_start) if quarter else price)
            query_start = chunk_end
        prices.sort(key=lambda p: (p.start, p.zone))
        return prices


def _format(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise SourceError(f"{SOURCE}: timestampUTC is missing or not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable timestampUTC {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SourceError(f"{SOURCE}: timestampUTC must be UTC: {value!r}")
    parsed = parsed.astimezone(UTC)
    if parsed.minute % 15 or parsed.second or parsed.microsecond:
        raise SourceError(f"{SOURCE}: timestampUTC {value!r} is not on a 15-minute boundary")
    if parsed < _HOURLY_END and parsed.minute:
        raise SourceError(f"{SOURCE}: hourly timestampUTC {value!r} is not on the hour")
    return parsed


def _number(record: dict[str, Any], column: str) -> float | None:
    return finite_number(record.get(column), source=SOURCE, column=column)


def _direction(record: dict[str, Any]) -> Direction | None:
    value = _number(record, "mainDirRegPowerPerMBA")
    if value is None:
        return None
    if value not in (-1, 0, 1):
        raise SourceError(f"{SOURCE}: unknown mainDirRegPowerPerMBA {value!r}")
    return Direction(int(value))


def _parse_imbalance(record: object) -> ImbalancePrice:
    if not isinstance(record, dict):
        raise SourceError(f"{SOURCE}: record is not a JSON object: {record!r}")
    area = record.get("mba")
    if not isinstance(area, str) or area not in BiddingZone.__members__:
        raise SourceError(f"{SOURCE}: unknown mba {area!r}")
    start = _parse_time(record.get("timestampUTC"))
    sales = _number(record, "imblSalesPrice")
    purchase = _number(record, "imblPurchasePrice")
    if sales is not None and purchase is not None and sales != purchase:
        raise SourceError(f"{SOURCE}: single-price model violated: sales != purchase")
    return ImbalancePrice(
        start=start,
        zone=BiddingZone[area],
        imbalance_price_eur=sales,
        imbalance_price_dkk=None,
        spot_price_eur=None,
        dominating_direction=_direction(record),
        satisfied_demand_mw=None,
        afrr_up_vwa_eur=None,
        afrr_down_vwa_eur=None,
        mfrr_up_price_eur=_number(record, "upRegPrice"),
        mfrr_down_price_eur=_number(record, "downRegPrice"),
        source=SOURCE,
        resolution=_HOUR if start < _HOURLY_END else INTERVAL,
        raw=record,
    )
