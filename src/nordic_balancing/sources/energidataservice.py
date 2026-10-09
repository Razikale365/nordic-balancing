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

from nordic_balancing.errors import RateLimitError, SourceError
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
_RETRY_STATUSES = frozenset({429, 503})
_DEFAULT_RETRY_AFTER = 10.0

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
        zone_list = sorted(set(zones))
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
        url = f"{self._base_url}{path}"
        for attempt in range(self._max_retries + 1):
            response = self._http.get(url, params=params)
            if response.status_code not in _RETRY_STATUSES:
                break
            retry_after = _retry_after(response)
            if attempt == self._max_retries:
                raise RateLimitError(SOURCE, retry_after)
            self._sleep(retry_after if retry_after is not None else _DEFAULT_RETRY_AFTER)
        if response.is_error:
            raise SourceError(
                f"{SOURCE}: HTTP {response.status_code} for {path}: {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError(f"{SOURCE}: response for {path} is not JSON") from exc
        if not isinstance(body, dict):
            raise SourceError(f"{SOURCE}: response for {path} is not a JSON object")
        return body


def _format(value: datetime) -> str:
    # The API accepts minute precision; seconds would be rejected.
    return value.strftime("%Y-%m-%dT%H:%M")


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        return None


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
    if not isinstance(area, str) or area not in BiddingZone.__members__:
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
        mfrr_marginal_up_eur=_number(record, "mFRRMarginalPriceUpEUR"),
        mfrr_marginal_down_eur=_number(record, "mFRRMarginalPriceDownEUR"),
        source=SOURCE,
    )
