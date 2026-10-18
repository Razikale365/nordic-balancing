"""Energinet's Energi Data Service (https://www.energidataservice.dk).

Free, no API key. Two behaviours of the API shape this client:

* ``start``/``end`` are read as **Danish local time** unless ``timezone=UTC``
  is sent, which silently shifts a naive query by one or two hours. This
  client always sends UTC.
* Rate limits are strict and per dataset: the API answers HTTP 429 with a
  ``Retry-After`` header. The client waits and retries a bounded number of
  times, then raises :class:`~nordic_balancing.errors.RateLimitError`.
"""

import json
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import httpx

from nordic_balancing._http import check_base_url, get_json
from nordic_balancing._validate import finite_number, reject_duplicate
from nordic_balancing.errors import SourceError
from nordic_balancing.models import (
    INTERVAL,
    BiddingZone,
    Direction,
    ImbalancePrice,
    ReserveCapacity,
    ReserveProduct,
    to_utc,
)

SOURCE = "energidataservice"
BASE_URL = "https://api.energidataservice.dk"
PAGE_SIZE = 10_000
_DK = frozenset({BiddingZone.DK1, BiddingZone.DK2})
_HOUR = timedelta(hours=1)
_FCR_ND_ZONES = frozenset(
    {BiddingZone.DK2, BiddingZone.SE1, BiddingZone.SE2, BiddingZone.SE3, BiddingZone.SE4}
)
_FCR_PRODUCTS = {
    "FCR-N": (ReserveProduct.FCR_N, Direction.NONE),
    "FCR-D upp": (ReserveProduct.FCR_D, Direction.UP),
    "FCR-D ned": (ReserveProduct.FCR_D, Direction.DOWN),
}
_FCR_ND_COLUMNS = (
    "HourUTC",
    "HourDK",
    "PriceArea",
    "ProductName",
    "AuctionType",
    "PurchasedVolumeLocal",
    "PurchasedVolumeTotal",
    "PriceTotalEUR",
)
_FCR_DK1_COLUMNS = (
    "HourUTC",
    "HourDK",
    "FCRdomestic_MW",
    "FCRabroad_MW",
    "FCRcross_EUR",
    "FCRcross_DKK",
    "FCRdk_EUR",
    "FCRdk_DKK",
)
_CAPACITY_DATASETS = {
    ReserveProduct.AFRR: "AfrrReservesNordic",
    ReserveProduct.MFRR: "mFRRCapacityMarket",
}
_CAPACITY_COLUMNS = (
    "TimeUTC",
    "TimeDK",
    "PriceArea",
    "UpDemandMW",
    "UpProcuredMW",
    "UpPriceEUR",
    "UpPriceDKK",
    "DownDemandMW",
    "DownProcuredMW",
    "DownPriceEUR",
    "DownPriceDKK",
)

_IMBALANCE_COLUMNS = (
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
)


class EnergiDataServiceClient:
    """Synchronous client for Energi Data Service datasets.

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
        zones: Iterable[BiddingZone] = (BiddingZone.DK1, BiddingZone.DK2),
    ) -> list[ImbalancePrice]:
        """Imbalance prices for intervals starting in ``[start, end)``.

        Data begins 2025-03-04 (dataset ``ImbalancePrice``). Results are
        sorted by interval start, then zone.
        """
        start_utc = to_utc(start, "start")
        end_utc = to_utc(end, "end")
        if end_utc <= start_utc:
            raise ValueError("end must be after start")
        requested = list(zones)
        if any(not isinstance(z, BiddingZone) or z not in _DK for z in requested):
            raise ValueError("zones must contain only DK1 and DK2 BiddingZone values")
        zone_list = sorted(set(requested))
        if not zone_list:
            raise ValueError("zones must not be empty")

        # The API reads start/end at minute precision: round the query outwards,
        # then filter the records back to the requested [start, end).
        query_start = start_utc.replace(second=0, microsecond=0)
        query_end = end_utc.replace(second=0, microsecond=0)
        if query_end < end_utc:
            query_end += timedelta(minutes=1)
        params = {
            "start": _format(query_start),
            "end": _format(query_end),
            "timezone": "UTC",
            "filter": json.dumps({"PriceArea": [z.value for z in zone_list]}),
            "columns": ",".join(_IMBALANCE_COLUMNS),
            "sort": "TimeUTC asc,PriceArea asc",
        }
        prices: list[ImbalancePrice] = []
        seen: set[tuple[BiddingZone, datetime]] = set()
        for record in self._records("ImbalancePrice", params):
            price = _parse_imbalance(record)
            if price.zone not in zone_list:
                raise SourceError(f"{SOURCE}: unrequested PriceArea {price.zone.value!r}")
            if not query_start <= price.start < query_end:
                raise SourceError(f"{SOURCE}: TimeUTC outside requested window")
            reject_duplicate(
                seen,
                (price.zone, price.start),
                source=SOURCE,
                detail=f"record for {price.zone.value} at {price.start.isoformat()}",
            )
            if start_utc <= price.start < end_utc:
                prices.append(price)
        prices.sort(key=lambda p: (p.start, p.zone))
        return prices

    def capacity_market(
        self,
        product: ReserveProduct,
        start: datetime,
        end: datetime,
        *,
        zones: Iterable[BiddingZone] = (BiddingZone.DK1, BiddingZone.DK2),
    ) -> list[ReserveCapacity]:
        """Return hourly capacity expanded to quarters starting in ``[start, end)``.

        aFRR covers all Nordic zones from 2022-12-07T23:00Z; mFRR covers DK1/DK2
        from 2023-06-20T22:00Z. Volumes are procured MW including cross-border
        allocation, not demand. Prices are marginal EUR/MW per hour. Demand and
        DKK prices remain in ``raw``. FCR-N/FCR-D cover DK2/SE1-SE4, emitting
        only Total rows with per-auction rows in ``raw["auctions"]``; FCR covers
        DK1 domestic capacity. FCR-N/FCR are symmetric (``Direction.NONE``).
        Pass explicit covered zones for FCR products. Results sort by start,
        zone and direction.
        """
        if not isinstance(product, ReserveProduct):
            raise ValueError("product must be a ReserveProduct")
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
        if product is ReserveProduct.MFRR and any(z not in _DK for z in zone_list):
            raise ValueError("Energi Data Service mFRR capacity covers only DK1 and DK2")
        if product in (ReserveProduct.FCR_N, ReserveProduct.FCR_D) and any(
            z not in _FCR_ND_ZONES for z in zone_list
        ):
            raise ValueError(
                f"Energi Data Service {product.value} capacity covers only DK2, SE1, SE2, "
                "SE3 and SE4; pass zones=(BiddingZone.DK2,) or covered SE zones"
            )
        if product is ReserveProduct.FCR and zone_list != [BiddingZone.DK1]:
            raise ValueError(
                "Energi Data Service FCR capacity covers only DK1; pass zones=(BiddingZone.DK1,)"
            )
        query_start = start_utc.replace(minute=0, second=0, microsecond=0)
        query_end = end_utc.replace(second=0, microsecond=0)
        if query_end < end_utc:
            query_end += timedelta(minutes=1)
        if product in (ReserveProduct.FCR_N, ReserveProduct.FCR_D, ReserveProduct.FCR):
            return self._fcr_capacity(
                product, start_utc, end_utc, zone_list, query_start, query_end
            )
        params = {
            "start": _format(query_start),
            "end": _format(query_end),
            "timezone": "UTC",
            "filter": json.dumps({"PriceArea": [z.value for z in zone_list]}),
            "columns": ",".join(_CAPACITY_COLUMNS),
            "sort": "TimeUTC asc,PriceArea asc",
        }
        capacities: list[ReserveCapacity] = []
        seen: set[tuple[datetime, BiddingZone]] = set()
        for record in self._records(_CAPACITY_DATASETS[product], params):
            if not isinstance(record, dict):
                raise SourceError(f"{SOURCE}: record is not a JSON object")
            missing = set(_CAPACITY_COLUMNS) - record.keys()
            if missing:
                raise SourceError(f"{SOURCE}: missing capacity columns {sorted(missing)}")
            hour = _parse_time(record["TimeUTC"])
            if hour.minute:
                raise SourceError(f"{SOURCE}: TimeUTC is not on the hour")
            if not query_start <= hour < query_end:
                raise SourceError(f"{SOURCE}: TimeUTC outside requested window")
            area = record["PriceArea"]
            zone = BiddingZone.__members__.get(area) if isinstance(area, str) else None
            if zone is None or zone not in zone_list:
                raise SourceError(f"{SOURCE}: unrequested PriceArea {area!r}")
            reject_duplicate(
                seen,
                (hour, zone),
                source=SOURCE,
                detail=f"record for {hour.isoformat()} {zone}",
            )
            numbers = {column: _number(record, column) for column in _CAPACITY_COLUMNS[3:]}
            for direction, prefix in ((Direction.UP, "Up"), (Direction.DOWN, "Down")):
                for quarter in range(4):
                    quarter_start = hour + quarter * INTERVAL
                    if start_utc <= quarter_start < end_utc:
                        capacities.append(
                            ReserveCapacity(
                                start=quarter_start,
                                zone=zone,
                                product=product,
                                direction=direction,
                                price_eur_per_mw=numbers[f"{prefix}PriceEUR"],
                                volume_mw=numbers[f"{prefix}ProcuredMW"],
                                source=SOURCE,
                                resolution=_HOUR,
                                raw=record,
                            )
                        )
        capacities.sort(key=lambda c: (c.start, c.zone, c.direction))
        return capacities

    def _fcr_capacity(
        self,
        product: ReserveProduct,
        start: datetime,
        end: datetime,
        zones: list[BiddingZone],
        query_start: datetime,
        query_end: datetime,
    ) -> list[ReserveCapacity]:
        domestic = product is ReserveProduct.FCR
        columns = _FCR_DK1_COLUMNS if domestic else _FCR_ND_COLUMNS
        numeric = columns[2:] if domestic else columns[5:]
        params = {
            "start": _format(query_start),
            "end": _format(query_end),
            "timezone": "UTC",
            "columns": ",".join(columns),
            # Offset paging needs a sort key that is unique per row: FcrNdDK2 has one row per
            # hour, area, product and auction type, and ties may come back in a different
            # order on each page request.
            "sort": "HourUTC asc"
            if domestic
            else "HourUTC asc,PriceArea asc,ProductName asc,AuctionType asc",
        }
        if not domestic:
            names = [
                name for name, (row_product, _) in _FCR_PRODUCTS.items() if row_product is product
            ]
            params["filter"] = json.dumps(
                {"PriceArea": [z.value for z in zones], "ProductName": names}
            )
            # On the October fall-back day FcrNdDK2 has no 00:00 UTC hour: its two 02:00
            # Danish hours are published as HourUTC 01:00 (really 00:00) and 01:01 (really
            # 01:00). Always fetch both of that day's first two hours so the gap is checked.
            for year in {query_start.year, query_end.year}:
                day = _fall_back_day(year)
                if query_start < day + 2 * _HOUR and query_end > day:
                    query_start = min(query_start, day)
                    query_end = max(query_end, day + 2 * _HOUR)
            params["start"] = _format(query_start)
            params["end"] = _format(query_end)
        parsed: list[tuple[datetime, BiddingZone, str, dict[str, Any]]] = []
        dataset = "FcrDK1" if domestic else "FcrNdDK2"
        # Unlike the other paths, rows are buffered before grouping, so the
        # duplicate check runs while paging: a server ignoring `offset` then
        # raises on page two instead of requesting and buffering forever.
        seen_rows: set[tuple[datetime, BiddingZone, str, str]] = set()
        for row in self._records(dataset, params):
            if not isinstance(row, dict):
                raise SourceError(f"{SOURCE}: record is not a JSON object")
            missing = set(columns) - row.keys()
            if missing:
                raise SourceError(f"{SOURCE}: missing capacity columns {sorted(missing)}")
            hour = None if domestic else _repeated_hour(row)
            if hour is None:
                hour = _parse_time(row["HourUTC"], column="HourUTC")
                if hour.minute:
                    raise SourceError(f"{SOURCE}: HourUTC is not on the hour")
            if not query_start <= hour < query_end:
                raise SourceError(f"{SOURCE}: HourUTC outside requested window")
            area = "DK1" if domestic else row["PriceArea"]
            zone = BiddingZone.__members__.get(area) if isinstance(area, str) else None
            if zone is None or zone not in zones:
                raise SourceError(f"{SOURCE}: unrequested PriceArea {area!r}")
            name = "FCR" if domestic else row["ProductName"]
            if not domestic and (not isinstance(name, str) or name not in _FCR_PRODUCTS):
                raise SourceError(f"{SOURCE}: unknown ProductName {name!r}")
            if not domestic and _FCR_PRODUCTS[name][0] is not product:
                raise SourceError(f"{SOURCE}: unrequested ProductName {name!r}")
            reject_duplicate(
                seen_rows,
                (hour, zone, name, json.dumps(row.get("AuctionType"), sort_keys=True)),
                source=SOURCE,
                detail=f"record for {hour.isoformat()} {zone} {name}",
            )
            for column in numeric:
                _number(row, column)
            parsed.append((hour, zone, name, row))
        # Days with a 01:01 row: shift that day's 01:00 rows to 00:00 and 01:01 to 01:00,
        # but only when the 00:00 hour they stand in for is really missing.
        repeated = {hour.replace(minute=0) - _HOUR for hour, *_ in parsed if hour.minute}
        if any(hour in repeated for hour, *_ in parsed):
            raise SourceError(f"{SOURCE}: HourUTC 01:01 published alongside 00:00 on the same day")
        groups: dict[tuple[datetime, BiddingZone, str], list[dict[str, Any]]] = {}
        for hour, zone, name, row in parsed:
            if hour.minute:
                hour = hour.replace(minute=0)
            elif hour - _HOUR in repeated:
                if row["HourDK"] != f"{hour:%Y-%m-%d}T02:00:00":
                    raise SourceError(
                        f"{SOURCE}: HourDK {row['HourDK']!r} for HourUTC 01:00 on a day with 01:01"
                    )
                hour -= _HOUR
            groups.setdefault((hour, zone, name), []).append(row)
        capacities: list[ReserveCapacity] = []
        seen: set[tuple[datetime, BiddingZone, str]] = set()
        for key, rows in groups.items():
            hour, zone, name = key
            direction = Direction.NONE if domestic else _FCR_PRODUCTS[name][1]
            totals = rows if domestic else [r for r in rows if r["AuctionType"] == "Total"]
            if not totals:
                raise SourceError(f"{SOURCE}: missing Total for {hour.isoformat()} {zone} {name}")
            for _ in totals:
                reject_duplicate(seen, key, source=SOURCE, detail=f"Total for {hour} {zone} {name}")
            row = totals[0]
            raw = dict(row)
            if not domestic:
                auctions: set[str] = set()
                raw["auctions"] = []
                for r in rows:
                    if r["AuctionType"] == "Total":
                        continue
                    reject_duplicate(
                        auctions,
                        json.dumps(r["AuctionType"], sort_keys=True),
                        source=SOURCE,
                        detail=f"{r['AuctionType']} for {hour} {zone} {name}",
                    )
                    raw["auctions"].append(r)
            volume = _number(row, "FCRdomestic_MW" if domestic else "PurchasedVolumeLocal")
            price = _number(row, "FCRdk_EUR" if domestic else "PriceTotalEUR")
            for quarter in range(4):
                quarter_start = hour + quarter * INTERVAL
                if start <= quarter_start < end:
                    capacities.append(
                        ReserveCapacity(
                            start=quarter_start,
                            zone=zone,
                            product=product,
                            direction=direction,
                            price_eur_per_mw=price,
                            volume_mw=volume,
                            source=SOURCE,
                            resolution=_HOUR,
                            raw=raw,
                        )
                    )
        return sorted(capacities, key=lambda c: (c.start, c.zone, c.direction))

    def _records(self, dataset: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        # Offset paging is safe only because every query above sorts by a key unique
        # per row: time and area for ImbalancePrice and the aFRR/mFRR datasets, the
        # hour for FcrDK1, and hour, area, product and auction type for FcrNdDK2.
        # The API's `total` is the row count of that page, not of the whole query,
        # so only a short page marks the end of the data.
        offset = 0
        while True:
            page = self._get(
                f"/dataset/{dataset}",
                {**params, "limit": str(PAGE_SIZE), "offset": str(offset)},
            )
            records = page.get("records")
            if not isinstance(records, list):
                raise SourceError(f"{SOURCE}: response for {dataset} has no 'records' list")
            total = page.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise SourceError(f"{SOURCE}: response for {dataset} has invalid total")
            if total != len(records):
                raise SourceError(
                    f"{SOURCE}: total {total} does not match {len(records)} records "
                    f"for {dataset} at offset {offset}"
                )
            offset += len(records)
            yield from records
            if len(records) < PAGE_SIZE:
                return

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            body = get_json(
                self._http,
                self._base_url,
                path,
                params,
                source=SOURCE,
                max_retries=self._max_retries,
                sleep=self._sleep,
            )
        except httpx.HTTPError:
            raise SourceError(f"{SOURCE}: request failed for {path}") from None
        if not isinstance(body, dict):
            raise SourceError(f"{SOURCE}: response for {path} is not a JSON object")
        return body


def _format(value: datetime) -> str:
    # The API accepts minute precision; seconds would be rejected.
    return value.strftime("%Y-%m-%dT%H:%M")


def _parse_time(value: object, *, column: str = "TimeUTC") -> datetime:
    if not isinstance(value, str):
        raise SourceError(f"{SOURCE}: {column} is missing or not a string: {value!r}")
    if "T" not in value and "t" not in value and " " not in value:
        raise SourceError(f"{SOURCE}: unparseable {column} {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError) as exc:
        raise SourceError(f"{SOURCE}: unparseable {column} {value!r}") from exc
    if parsed.tzinfo is not None and (
        parsed.utcoffset() != timedelta(0) or not value.endswith(("Z", "z", "+00:00"))
    ):
        raise SourceError(f"{SOURCE}: {column} must be UTC: {value!r}")
    parsed = parsed.replace(tzinfo=UTC)
    # Check every field: int(timestamp()) would drop fractional seconds (00:00:00.5).
    if parsed.minute % 15 or parsed.second or parsed.microsecond:
        raise SourceError(f"{SOURCE}: {column} {value!r} is not on a 15-minute boundary")
    return parsed


def _fall_back_day(year: int) -> datetime:
    """Return 00:00 UTC on the last Sunday of October, when EU clocks go back."""
    last = datetime(year, 10, 31, tzinfo=UTC)
    return last - timedelta(days=(last.weekday() + 1) % 7)


def _repeated_hour(row: dict[str, Any]) -> datetime | None:
    """Return 01:01 UTC for FcrNdDK2's extra row on the October fall-back day.

    Only the exact published pattern qualifies: HourUTC ``01:01:00`` with HourDK
    ``02:01:00`` on the last Sunday of October. Anything else returns ``None`` and
    goes through the usual 15-minute check. The caller maps 01:01 to 01:00.
    """
    value, local = row["HourUTC"], row["HourDK"]
    if not isinstance(value, str) or not isinstance(local, str):
        return None
    try:
        day = _fall_back_day(int(value[:4]))
    except ValueError:
        return None
    if value != f"{day:%Y-%m-%d}T01:01:00" or local != f"{day:%Y-%m-%d}T02:01:00":
        return None
    return day + timedelta(hours=1, minutes=1)


def _number(record: dict[str, Any], column: str) -> float | None:
    return finite_number(record.get(column), source=SOURCE, column=column)


def _direction(record: dict[str, Any]) -> Direction | None:
    value = _number(record, "DominatingDirection")
    if value is None:
        return None
    # Exact match: int() would silently truncate a fractional value (0.6 -> 0).
    if value not in (-1, 0, 1):
        raise SourceError(f"{SOURCE}: unknown DominatingDirection {value!r}")
    return Direction(int(value))


def _parse_imbalance(record: object) -> ImbalancePrice:
    if not isinstance(record, dict):
        raise SourceError(f"{SOURCE}: record is not a JSON object: {type(record).__name__}")
    missing = set(_IMBALANCE_COLUMNS) - record.keys()
    if missing:
        raise SourceError(f"{SOURCE}: missing imbalance columns {sorted(missing)}")
    area = record.get("PriceArea")
    if not isinstance(area, str) or area not in _DK:
        raise SourceError(f"{SOURCE}: unknown PriceArea {area!r}")
    zone = BiddingZone[area]
    return ImbalancePrice(
        start=_parse_time(record.get("TimeUTC")),
        zone=zone,
        imbalance_price_eur=_number(record, "ImbalancePriceEUR"),
        imbalance_price_dkk=_number(record, "ImbalancePriceDKK"),
        spot_price_eur=_number(record, "SpotPriceEUR"),
        dominating_direction=_direction(record),
        satisfied_demand_mw=_number(record, "SatisfiedDemand"),
        afrr_up_vwa_eur=_number(record, "aFRRVWAUpEUR"),
        afrr_down_vwa_eur=_number(record, "aFRRVWADownEUR"),
        mfrr_up_price_eur=_number(record, "mFRRMarginalPriceUpEUR"),
        mfrr_down_price_eur=_number(record, "mFRRMarginalPriceDownEUR"),
        source=SOURCE,
        resolution=INTERVAL,
        raw=record,
    )
