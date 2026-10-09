"""Fingrid open data, with API-key authentication and paced requests."""

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import httpx

from nordic_balancing._http import get_json
from nordic_balancing._validate import finite_number, reject_duplicate
from nordic_balancing.errors import SourceError
from nordic_balancing.models import INTERVAL, BiddingZone, Direction, ImbalancePrice, to_utc

SOURCE = "fingrid"
BASE_URL = "https://data.fingrid.fi/api"
PAGE_SIZE = 20_000
_HOUR = timedelta(hours=1)
# Imbalance and mFRR pricing became 15-minute at 2025-03-19 00:00 CET. Before that
# instant the hourly record is authoritative (eSett settled on it); after it, the
# 15-minute record is. A record that loses is kept in ``superseded``, never dropped.
_QUARTER_PRICING = datetime(2025, 3, 18, 23, tzinfo=UTC)


class _AuthenticatedTransport(httpx.BaseTransport):
    def __init__(
        self, http: httpx.Client, api_key: str, before_request: Callable[[], None]
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._before_request = before_request

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._before_request()
        response = self._http.get(request.url, headers={"x-api-key": self._api_key})
        return httpx.Response(
            response.status_code,
            headers={
                name: value
                for name, value in response.headers.items()
                if name not in ("content-encoding", "content-length")
            },
            content=b"" if response.is_error else response.content,
        )


@dataclass(frozen=True, slots=True)
class _Record:
    start: datetime
    value: float | None
    resolution: timedelta
    raw: dict[str, Any]
    superseded: tuple[dict[str, Any], ...] = ()


class FingridClient:
    """Synchronous client for Fingrid's datasets.

    ``api_key`` defaults to ``FINGRID_API_KEY``. Pass your own ``httpx.Client``
    to control proxies, timeouts or transports; otherwise this instance owns it.
    Requests, including retries, are paced by ``min_interval`` seconds.
    """

    def __init__(
        self,
        api_key: str | None = None,
        http: httpx.Client | None = None,
        *,
        base_url: str = BASE_URL,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        min_interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("FINGRID_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("api_key or FINGRID_API_KEY must be set")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if (
            isinstance(min_interval, bool)
            or not isinstance(min_interval, int | float)
            or not math.isfinite(min_interval)
            or min_interval < 0
        ):
            raise ValueError("min_interval must be a finite non-negative number")
        self._api_key = key
        self._owns_http = http is None
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep
        self._min_interval = min_interval
        self._clock = clock
        self._last_request: float | None = None
        # Keep authentication and pacing local, without changing the supplied client.
        self._authenticated_http = httpx.Client(
            transport=_AuthenticatedTransport(self._http, key, self._pace)
        )

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
        """Close internal resources and the HTTP client only if this instance owns it."""
        self._authenticated_http.close()
        if self._owns_http:
            self._http.close()

    def series(
        self, dataset_id: int, start: datetime, end: datetime
    ) -> list[tuple[datetime, float | None]]:
        """Return validated values starting in ``[start, end)``, in UTC order.

        Hourly records (published until 2025-03-18) are repeated across their
        four quarter-hour starts, so the series always has 15-minute steps.
        Where both granularities cover a quarter, the hourly value is used
        before 2025-03-18T23:00Z and the 15-minute value from then on.
        """
        return [(r.start, r.value) for r in self._records(dataset_id, start, end)]

    def imbalance_prices(self, start: datetime, end: datetime) -> list[ImbalancePrice]:
        """Join FI imbalance prices with mFRR up/down prices and dominating direction.

        Only starts published by dataset 319 are returned; hourly records are
        repeated across their four quarters with ``resolution`` kept at one
        hour. Missing component rows or null values remain ``None``. ``raw``
        contains rows keyed by dataset ID; a record that was overlapped by the
        other granularity (2025-03-14 to 2025-03-18) is kept under
        ``"<dataset>_superseded"``.
        """
        datasets = {
            dataset: {r.start: r for r in self._records(dataset, start, end)}
            for dataset in (319, 244, 106, 369)
        }
        directions = {stamp: _direction(r.value) for stamp, r in datasets[369].items()}
        prices: list[ImbalancePrice] = []
        for stamp, record in datasets[319].items():
            up = datasets[244].get(stamp)
            down = datasets[106].get(stamp)
            prices.append(
                ImbalancePrice(
                    start=stamp,
                    zone=BiddingZone.FI,
                    imbalance_price_eur=record.value,
                    imbalance_price_dkk=None,
                    spot_price_eur=None,
                    dominating_direction=directions.get(stamp),
                    satisfied_demand_mw=None,
                    afrr_up_vwa_eur=None,
                    afrr_down_vwa_eur=None,
                    mfrr_up_price_eur=up.value if up is not None else None,
                    mfrr_down_price_eur=down.value if down is not None else None,
                    source=SOURCE,
                    resolution=record.resolution,
                    raw={
                        **{
                            str(dataset): dict(rows[stamp].raw)
                            for dataset, rows in datasets.items()
                            if stamp in rows
                        },
                        **{
                            f"{dataset}_superseded": [dict(r) for r in rows[stamp].superseded]
                            for dataset, rows in datasets.items()
                            if stamp in rows and rows[stamp].superseded
                        },
                    },
                )
            )
        return prices

    def _pace(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            remaining = self._min_interval - (now - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._clock()

    def _get(self, path: str, params: dict[str, str]) -> Any:
        try:
            return get_json(
                self._authenticated_http,
                self._base_url,
                path,
                params,
                source=SOURCE,
                max_retries=self._max_retries,
                sleep=self._sleep,
            )
        except SourceError as exc:
            raise SourceError(str(exc).replace(self._api_key, "[REDACTED_SECRET]")) from None
        except httpx.HTTPError:
            raise SourceError(f"{SOURCE}: request failed for {path}") from None

    def _records(self, dataset_id: int, start: datetime, end: datetime) -> list[_Record]:
        if isinstance(dataset_id, bool) or not isinstance(dataset_id, int) or dataset_id <= 0:
            raise ValueError("dataset_id must be a positive integer")
        start_utc = to_utc(start, "start")
        end_utc = to_utc(end, "end")
        if end_utc <= start_utc:
            raise ValueError("end must be after start")
        # Floor the query start to the hour so an hourly record covering start
        # is fetched; round the end up so no 15-minute record start is missed.
        query_start = start_utc.replace(minute=0, second=0, microsecond=0)
        query_end = end_utc.replace(minute=end_utc.minute // 15 * 15, second=0, microsecond=0)
        if query_end < end_utc:
            query_end += INTERVAL
        params = {
            "startTime": query_start.isoformat().replace("+00:00", "Z"),
            "endTime": query_end.isoformat().replace("+00:00", "Z"),
            "pageSize": str(PAGE_SIZE),
        }
        path = f"/datasets/{dataset_id}/data"
        first = self._get(path, {**params, "page": "1"})
        rows = _page_rows(first)
        pagination = first.get("pagination")
        if not isinstance(pagination, dict):
            raise SourceError(f"{SOURCE}: page 1 has no pagination object")
        total = pagination.get("total")
        last_page = pagination.get("lastPage")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise SourceError(f"{SOURCE}: page 1 has invalid total")
        if isinstance(last_page, bool) or not isinstance(last_page, int) or last_page < 1:
            raise SourceError(f"{SOURCE}: page 1 has invalid lastPage")
        for page in range(2, last_page + 1):
            rows.extend(_page_rows(self._get(path, {**params, "page": str(page)})))
        if len(rows) != total:
            raise SourceError(
                f"{SOURCE}: row count {len(rows)} != page-1 total {total}; "
                "data changed or truncated"
            )
        records = [_parse_record(row, dataset_id) for row in rows]
        seen: set[tuple[datetime, timedelta]] = set()
        for record in records:
            if not query_start <= record.start < query_end:
                raise SourceError(f"{SOURCE}: record outside requested window")
            reject_duplicate(
                seen, (record.start, record.resolution), source=SOURCE, detail="interval start"
            )
        # Expand hourly records to quarter-hour starts. Around the 2025-03 switch
        # Fingrid published both granularities for the same quarters, sometimes
        # with different values; see _QUARTER_PRICING for which one is kept.
        expanded: dict[datetime, _Record] = {}
        for record in records:
            for quarter in range(4 if record.resolution == _HOUR else 1):
                start = record.start + quarter * INTERVAL
                candidate = record if quarter == 0 else replace(record, start=start)
                existing = expanded.get(start)
                if existing is None:
                    expanded[start] = candidate
                    continue
                preferred = _HOUR if start < _QUARTER_PRICING else INTERVAL
                winner, loser = (
                    (candidate, existing)
                    if candidate.resolution == preferred
                    else (existing, candidate)
                )
                expanded[start] = replace(
                    winner, superseded=(*winner.superseded, loser.raw, *loser.superseded)
                )
        return sorted(
            (r for r in expanded.values() if start_utc <= r.start < end_utc),
            key=lambda r: r.start,
        )


def _page_rows(body: Any) -> list[Any]:
    if not isinstance(body, dict):
        raise SourceError(f"{SOURCE}: response is not a JSON object")
    rows = body.get("data")
    if not isinstance(rows, list):
        raise SourceError(f"{SOURCE}: response has no 'data' list")
    return list(rows)


def _parse_time(value: object, column: str) -> datetime:
    if not isinstance(value, str):
        raise SourceError(f"{SOURCE}: {column} is missing or not a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise SourceError(f"{SOURCE}: unparseable {column}") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SourceError(f"{SOURCE}: {column} must be UTC")
    if parsed.minute % 15 or parsed.second or parsed.microsecond:
        raise SourceError(f"{SOURCE}: {column} is not on a 15-minute boundary")
    return parsed.astimezone(UTC)


def _parse_record(record: object, dataset_id: int) -> _Record:
    if not isinstance(record, dict):
        raise SourceError(f"{SOURCE}: record is not a JSON object")
    actual_id = record.get("datasetId")
    if isinstance(actual_id, bool) or not isinstance(actual_id, int) or actual_id != dataset_id:
        raise SourceError(f"{SOURCE}: record has an unexpected datasetId")
    start = _parse_time(record.get("startTime"), "startTime")
    end = _parse_time(record.get("endTime"), "endTime")
    duration = end - start
    if duration not in (INTERVAL, _HOUR):
        raise SourceError(f"{SOURCE}: record duration must be 15 or 60 minutes")
    if "value" not in record:
        raise SourceError(f"{SOURCE}: value is missing")
    number = finite_number(record["value"], source=SOURCE, column="value")
    return _Record(start, number, _HOUR if duration == _HOUR else INTERVAL, dict(record))


def _direction(value: float | None) -> Direction | None:
    # GET /datasets/369: -1 = down, 0 = no direction, 1 = up.
    if value is None:
        return None
    if value not in (-1, 0, 1):
        raise SourceError(f"{SOURCE}: unknown dominating direction")
    return Direction(int(value))
