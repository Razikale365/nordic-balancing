"""Keyless eSett single imbalance prices, preserving publication resolution."""

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from operator import attrgetter
from types import TracebackType
from typing import Any, Self

import httpx

from nordic_balancing._http import check_base_url, get_json
from nordic_balancing._validate import finite_number
from nordic_balancing.errors import SourceError
from nordic_balancing.models import (
    INTERVAL,
    BiddingZone,
    Direction,
    ImbalancePrice,
    _owned_imbalance_price,
    to_utc,
)

SOURCE = "esett"
BASE_URL = "https://api.opendata.esett.com"
_HOURLY_END = datetime(2023, 5, 21, 22, tzinfo=UTC)
_HOUR = timedelta(hours=1)
_CHUNK = timedelta(days=31)
# eSett sends every column, with null for an unpublished value, so a missing key means
# the schema changed; reading it as None would report data as unpublished.
_VALUE_KEYS = (
    "imblSalesPrice",
    "imblPurchasePrice",
    "upRegPrice",
    "downRegPrice",
    "mainDirRegPowerPerMBA",
)
_VALUE_KEY_SET = frozenset(_VALUE_KEYS)
_ZONES = dict(BiddingZone.__members__)
_DIRECTIONS = {float(d): d for d in Direction}
# DK1/DK2 mark an interval that is not settled yet with 0.0 in every price
# column instead of null (and a non-zero imblSpotDifferencePrice); SE, NO and
# FI use null, so the signature is DK-only.
_DK_ZONES = frozenset({BiddingZone.DK1, BiddingZone.DK2})
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
        check_base_url(base_url)
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
        zone_set = set(zone_list)
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
        # All zones share each timestamp string, so parse it once per call.
        times: dict[str, datetime] = {}
        while query_start < query_end:
            # Boundaries between chunks sit on the quarter-hour grid: eSett leaves
            # out the interval at an unaligned boundary from both neighbouring chunks.
            step = query_start + _CHUNK
            step -= timedelta(
                minutes=step.minute % 15, seconds=step.second, microseconds=step.microsecond
            )
            chunk_end = min(step, query_end)
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
            hourly_hours: dict[BiddingZone, set[datetime]] = {}
            quarter_zones: set[BiddingZone] = set()
            for record in body:
                price = _parse_imbalance(record, times)
                zone = price.zone
                start = price.start
                if zone not in zone_set:
                    raise SourceError(f"{SOURCE}: unrequested mba {zone.value!r}")
                if not query_start <= start < chunk_end:
                    raise SourceError(f"{SOURCE}: timestampUTC outside requested chunk")
                key = (zone, start)
                if key in seen:
                    raise SourceError(
                        f"{SOURCE}: duplicate record for {zone.value} at {start.isoformat()}"
                    )
                seen.add(key)
                if start >= _HOURLY_END:
                    # Quarter-hour resolution: the record is its own single interval.
                    if start.minute:
                        quarter_zones.add(zone)
                    else:
                        hourly_hours.setdefault(zone, set()).add(start)
                    if start_utc <= start < end_utc:
                        prices.append(price)
                    continue
                for quarter in range(4):
                    interval_start = price.start + quarter * INTERVAL
                    if start_utc <= interval_start < end_utc:
                        prices.append(replace(price, start=interval_start) if quarter else price)
            # A zone publishing again at hourly cadence lands on the hour in
            # every hour; sparse 15-minute data still shows a quarter somewhere.
            # Returning one record per hour labelled 15 minutes would be wrong.
            for zone, hours in hourly_hours.items():
                if len(hours) > 1 and zone not in quarter_zones:
                    raise SourceError(
                        f"{SOURCE}: {zone.value} rows after {_HOURLY_END:%Y-%m-%dT%H:%MZ} "
                        "fall on the hour only"
                    )
            query_start = chunk_end
        prices.sort(key=attrgetter("start", "zone"))
        return prices


def _format(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _short(value: object) -> str:
    """repr() of a provider value, cut so a huge field stays a short message."""
    text = repr(value)
    return text if len(text) <= 64 else text[:64] + "..."


def _parse_time(value: object, cache: dict[str, datetime]) -> datetime:
    if type(value) is str and (cached := cache.get(value)) is not None:
        return cached
    if not isinstance(value, str):
        raise SourceError(f"{SOURCE}: timestampUTC is missing or not a string: {_short(value)}")
    if "T" not in value and "t" not in value and " " not in value:
        raise SourceError(f"{SOURCE}: unparseable timestampUTC {_short(value)}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable timestampUTC {_short(value)}") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or not value.endswith(("Z", "z", "+00:00"))
    ):
        raise SourceError(f"{SOURCE}: timestampUTC must be UTC: {_short(value)}")
    parsed = parsed.astimezone(UTC)
    if parsed.minute % 15 or parsed.second or parsed.microsecond:
        raise SourceError(f"{SOURCE}: timestampUTC {_short(value)} is not on a 15-minute boundary")
    if parsed < _HOURLY_END and parsed.minute:
        raise SourceError(f"{SOURCE}: hourly timestampUTC {_short(value)} is not on the hour")
    cache[value] = parsed
    return parsed


def _number(record: dict[str, Any], column: str) -> float | None:
    value = record.get(column)
    # Fast path for what JSON decoding produces; finite_number handles the rest.
    if value is None:
        return None
    if type(value) is float and math.isfinite(value):
        return value
    return finite_number(value, source=SOURCE, column=column)


def _is_zero(value: object) -> bool:
    # Only a published number counts: a null (or anything else) is no evidence
    # of a settled price, so the row stays unpublished.
    return type(value) in (int, float) and value == 0


def _direction(record: dict[str, Any]) -> Direction | None:
    value = _number(record, "mainDirRegPowerPerMBA")
    if value is None:
        return None
    direction = _DIRECTIONS.get(value)
    if direction is None:
        raise SourceError(f"{SOURCE}: unknown mainDirRegPowerPerMBA {_short(value)}")
    return direction


def _parse_imbalance(record: object, times: dict[str, datetime]) -> ImbalancePrice:
    if not isinstance(record, dict):
        raise SourceError(f"{SOURCE}: record is not a JSON object: {_short(record)}")
    area = record.get("mba")
    zone = _ZONES.get(area) if isinstance(area, str) else None
    if zone is None:
        raise SourceError(f"{SOURCE}: unknown mba {_short(area)}")
    start = _parse_time(record.get("timestampUTC"), times)
    if not record.keys() >= _VALUE_KEY_SET:
        missing = [key for key in _VALUE_KEYS if key not in record]
        raise SourceError(f"{SOURCE}: record is missing {', '.join(missing)}")
    sales = record["imblSalesPrice"]
    purchase = record["imblPurchasePrice"]
    up = record["upRegPrice"]
    down = record["downRegPrice"]
    flow = record["mainDirRegPowerPerMBA"]
    # A settled row is five plain floats; if their sum is finite, so is each of them
    # (an overflowing sum falls through), so every column check below would pass.
    if type(sales) is type(purchase) is type(up) is type(down) is type(flow) is float and (
        math.isfinite(sales + purchase + up + down + flow)
    ):
        if sales != purchase:
            raise SourceError(f"{SOURCE}: single-price model violated: sales != purchase")
        direction = _DIRECTIONS.get(flow)
        if direction is None:
            raise SourceError(f"{SOURCE}: unknown mainDirRegPowerPerMBA {_short(flow)}")
    else:
        # Same checks one column at a time, in the order that decides which error wins.
        sales = _number(record, "imblSalesPrice")
        purchase = _number(record, "imblPurchasePrice")
        if sales is not None and purchase is not None and sales != purchase:
            raise SourceError(f"{SOURCE}: single-price model violated: sales != purchase")
        up = _number(record, "upRegPrice")
        down = _number(record, "downRegPrice")
        direction = _direction(record)
    # Single-price model: sales and purchase are the same price by definition, so a
    # price published in only one column is that price (never observed in ~107k
    # records, 2021-11 to 2026-10, but it must not be dropped as None).
    single = sales if sales is not None else purchase
    if (
        zone in _DK_ZONES
        and sales == purchase == up == down == 0.0
        and not _is_zero(record.get("imblSpotDifferencePrice"))
    ):
        # The pre-settlement DK placeholder is not a price; report it as
        # unpublished, like the null rows the other zones send for it. Its
        # direction may be -1, 0 or 1, so only the spot difference tells it
        # apart: a settled price of 0.0 differs from spot only when spot is not
        # 0.0 too, and every all-zero settled row seen (34 in 2025-2026, all
        # matching Energi Data Service) has a difference of exactly 0.0, while
        # every placeholder seen has minus the spot price there.
        single = up = down = direction = None
    # start comes from _parse_time (a plain UTC datetime) and record was decoded for
    # this call alone; hourly quarters are copied from it through dataclasses.replace.
    return _owned_imbalance_price(
        start=start,
        zone=zone,
        imbalance_price_eur=single,
        mfrr_up_price_eur=up,
        mfrr_down_price_eur=down,
        dominating_direction=direction,
        source=SOURCE,
        resolution=_HOUR if start < _HOURLY_END else INTERVAL,
        raw=record,
    )
