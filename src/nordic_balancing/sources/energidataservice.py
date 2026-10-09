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
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

import httpx

from nordic_balancing._http import get_json
from nordic_balancing.errors import SourceError
from nordic_balancing.models import (
    INTERVAL,
    BiddingZone,
    Direction,
    ImbalancePrice,
    to_utc,
)

SOURCE = "energidataservice"
BASE_URL = "https://api.energidataservice.dk"
PAGE_SIZE = 10_000
_DK = frozenset({BiddingZone.DK1, BiddingZone.DK2})

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

        params = {
            "start": _format(start_utc),
            "end": _format(end_utc),
            "timezone": "UTC",
            "filter": json.dumps({"PriceArea": [z.value for z in zone_list]}),
            "columns": ",".join(_IMBALANCE_COLUMNS),
            "sort": "TimeUTC asc,PriceArea asc",
        }
        prices = [_parse_imbalance(r) for r in self._records("ImbalancePrice", params)]
        prices.sort(key=lambda p: (p.start, p.zone))
        return prices

    def _records(self, dataset: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            page = self._get(
                f"/dataset/{dataset}",
                {**params, "limit": str(PAGE_SIZE), "offset": str(offset)},
            )
            records = page.get("records")
            if not isinstance(records, list):
                raise SourceError(f"{SOURCE}: response for {dataset} has no 'records' list")
            yield from records
            offset += len(records)
            total = page.get("total")
            if not records or not isinstance(total, int) or offset >= total:
                return

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        body = get_json(
            self._http,
            self._base_url,
            path,
            params,
            source=SOURCE,
            max_retries=self._max_retries,
            sleep=self._sleep,
        )
        if not isinstance(body, dict):
            raise SourceError(f"{SOURCE}: response for {path} is not a JSON object")
        return body


def _format(value: datetime) -> str:
    # The API accepts minute precision; seconds would be rejected.
    return value.strftime("%Y-%m-%dT%H:%M")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise SourceError(f"{SOURCE}: TimeUTC is missing or not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable TimeUTC {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    parsed = parsed.replace(tzinfo=UTC)
    epoch_seconds = int(parsed.timestamp())
    if epoch_seconds % int(INTERVAL.total_seconds()):
        raise SourceError(f"{SOURCE}: TimeUTC {value!r} is not on a 15-minute boundary")
    return parsed


def _number(record: dict[str, Any], column: str) -> float | None:
    value = record.get(column)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SourceError(f"{SOURCE}: {column} is not a number: {value!r}")
    return float(value)


def _direction(record: dict[str, Any]) -> Direction | None:
    value = _number(record, "DominatingDirection")
    if value is None:
        return None
    try:
        return Direction(int(value))
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unknown DominatingDirection {value!r}") from exc


def _parse_imbalance(record: dict[str, Any]) -> ImbalancePrice:
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
