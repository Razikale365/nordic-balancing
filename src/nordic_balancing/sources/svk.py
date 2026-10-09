"""Keyless Svenska kraftnät (CKAN) reserve capacity market results."""

import json
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import httpx

from nordic_balancing._http import get_json
from nordic_balancing.errors import SourceError
from nordic_balancing.models import (
    INTERVAL,
    BiddingZone,
    Direction,
    ReserveCapacity,
    ReserveProduct,
    to_utc,
)

SOURCE = "svk"
BASE_URL = "https://data.svk.se/api/3/action"
_HOUR = timedelta(hours=1)
_CHUNK_HOURS = 168
_PAGE_LIMIT = 10_000
_SE_ZONES = (BiddingZone.SE1, BiddingZone.SE2, BiddingZone.SE3, BiddingZone.SE4)
_DATASETS = {
    ReserveProduct.MFRR: ("mfrr_capacity_market", "mFRRCapacityMarket"),
    ReserveProduct.AFRR: ("afrr_capacity_market", "aFRRCapacityMarket"),
}
_DIRECTIONS = {"up": Direction.UP, "down": Direction.DOWN}


class SvKClient:
    """Synchronous SvK client with bounded requests and no authentication.

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
        self._resource_ids: dict[ReserveProduct, str] = {}

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

    def capacity_market(
        self,
        product: ReserveProduct,
        start: datetime,
        end: datetime,
        zones: Iterable[BiddingZone] = _SE_ZONES,
    ) -> list[ReserveCapacity]:
        """Return hourly capacity records expanded to intervals in ``[start, end)``.

        SvK publishes one price and procured volume per hour, bidding zone and
        direction; each record is repeated across four quarters with its hourly
        resolution retained. Results are sorted by start, zone and direction.
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
        if any(z not in _SE_ZONES for z in zone_list):
            raise ValueError("SvK capacity markets cover only SE1-SE4")
        hours: list[datetime] = []
        hour = start_utc.replace(minute=0, second=0, microsecond=0)
        while hour < end_utc:
            hours.append(hour)
            hour += _HOUR
        resource_id = self._resource_id(product)
        seen: set[tuple[datetime, BiddingZone, Direction]] = set()
        capacities: list[ReserveCapacity] = []
        for offset in range(0, len(hours), _CHUNK_HOURS):
            chunk = set(hours[offset : offset + _CHUNK_HOURS])
            for record in self._records(resource_id, product, sorted(chunk), zone_list):
                capacity = _parse_record(record, product, zone_list, chunk, seen)
                for quarter in range(4):
                    quarter_start = capacity.start + quarter * INTERVAL
                    if start_utc <= quarter_start < end_utc:
                        capacities.append(
                            replace(capacity, start=quarter_start) if quarter else capacity
                        )
        capacities.sort(key=lambda c: (c.start, c.zone, c.direction))
        return capacities

    def _resource_id(self, product: ReserveProduct) -> str:
        cached = self._resource_ids.get(product)
        if cached is not None:
            return cached
        dataset = _DATASETS[product][0]
        result = _result(self._get("/package_show", {"id": dataset}), "/package_show")
        resources = result.get("resources")
        if not isinstance(resources, list):
            raise SourceError(f"{SOURCE}: package_show for {dataset} has no resources list")
        for resource in resources:
            if isinstance(resource, dict) and resource.get("datastore_active") is True:
                resource_id = resource.get("id")
                if isinstance(resource_id, str):
                    self._resource_ids[product] = resource_id
                    return resource_id
        raise SourceError(f"{SOURCE}: no datastore resource for {dataset}")

    def _records(
        self,
        resource_id: str,
        product: ReserveProduct,
        chunk: list[datetime],
        zones: list[BiddingZone],
    ) -> list[Any]:
        params = {
            "resource_id": resource_id,
            "filters": json.dumps(
                {
                    "start_time_utc": [h.strftime("%Y-%m-%dT%H:00:00") for h in chunk],
                    "bidding_zone": [z.value for z in zones],
                }
            ),
            "limit": str(_PAGE_LIMIT),
            "sort": "start_time_utc,bidding_zone,reserve_direction",
        }
        rows: list[Any] = []
        while True:
            result = _result(
                self._get("/datastore_search", {**params, "offset": str(len(rows))}),
                "/datastore_search",
            )
            total = result.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise SourceError(f"{SOURCE}: datastore_search has invalid total")
            records = result.get("records")
            if not isinstance(records, list):
                raise SourceError(f"{SOURCE}: datastore_search has no records list")
            if not records and len(rows) < total:
                raise SourceError(f"{SOURCE}: datastore_search ended before reaching total")
            rows.extend(records)
            if len(rows) >= total:
                break
        if len(rows) != total:
            raise SourceError(
                f"{SOURCE}: row count {len(rows)} != total {total}; data changed or truncated"
            )
        return rows

    def _get(self, path: str, params: dict[str, str]) -> Any:
        try:
            return get_json(
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


def _result(body: Any, path: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise SourceError(f"{SOURCE}: response for {path} is not a JSON object")
    if body.get("success") is not True:
        raise SourceError(f"{SOURCE}: {path} reported failure")
    result = body.get("result")
    if not isinstance(result, dict):
        raise SourceError(f"{SOURCE}: response for {path} has no result object")
    return result


def _number(record: dict[str, Any], column: str) -> float | None:
    value = record.get(column)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SourceError(f"{SOURCE}: {column} is not a number: {value!r}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise SourceError(f"{SOURCE}: {column} is not finite: {value!r}") from exc
    if not math.isfinite(number):
        raise SourceError(f"{SOURCE}: {column} is not finite: {value!r}")
    return number


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise SourceError(f"{SOURCE}: start_time_utc is missing or not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable start_time_utc {value!r}") from exc
    if parsed.tzinfo is not None:
        raise SourceError(f"{SOURCE}: start_time_utc must be naive UTC: {value!r}")
    if parsed.minute or parsed.second or parsed.microsecond:
        raise SourceError(f"{SOURCE}: start_time_utc {value!r} is not on the hour")
    return parsed.replace(tzinfo=UTC)


def _parse_record(
    record: object,
    product: ReserveProduct,
    zones: list[BiddingZone],
    chunk: set[datetime],
    seen: set[tuple[datetime, BiddingZone, Direction]],
) -> ReserveCapacity:
    if not isinstance(record, dict):
        raise SourceError(f"{SOURCE}: record is not a JSON object: {record!r}")
    actual_product = record.get("reserve_product")
    if actual_product != _DATASETS[product][1]:
        raise SourceError(f"{SOURCE}: unexpected reserve_product {actual_product!r}")
    zone_value = record.get("bidding_zone")
    zone = BiddingZone.__members__.get(zone_value) if isinstance(zone_value, str) else None
    if zone is None or zone not in zones:
        raise SourceError(f"{SOURCE}: not a requested bidding_zone {zone_value!r}")
    direction_value = record.get("reserve_direction")
    direction = _DIRECTIONS.get(direction_value) if isinstance(direction_value, str) else None
    if direction is None:
        raise SourceError(f"{SOURCE}: unknown reserve_direction {direction_value!r}")
    start = _parse_time(record.get("start_time_utc"))
    if start not in chunk:
        raise SourceError(f"{SOURCE}: start_time_utc outside requested chunk")
    if record.get("price_unit") != "EUR-MW":
        raise SourceError(f"{SOURCE}: unexpected price_unit {record.get('price_unit')!r}")
    if record.get("volume_unit") != "MW":
        raise SourceError(f"{SOURCE}: unexpected volume_unit {record.get('volume_unit')!r}")
    key = (start, zone, direction)
    if key in seen:
        raise SourceError(f"{SOURCE}: duplicate record for {start.isoformat()} {zone} {direction}")
    seen.add(key)
    return ReserveCapacity(
        start=start,
        zone=zone,
        product=product,
        direction=direction,
        price_eur_per_mw=_number(record, "price"),
        volume_mw=_number(record, "volume"),
        source=SOURCE,
        resolution=_HOUR,
        raw=record,
    )
