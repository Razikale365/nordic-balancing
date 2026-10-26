"""ENTSO-E Transparency Platform imbalance prices (A85) and FCR-N/mFRR capacity (A81).

The XML structure handled here was verified against the official IEC
62325-451-6 balancing document schema, namespace versions 3.0 to 4.5, using
the XSDs published in ENTSO-E's EDI library (CIM_xsd_package_v2026) and the
generated models in entsoe-apy. In that schema a ``Point`` requires only
``position``; ``imbalance_Price.amount``, ``imbalance_Price.category``,
``flowDirection.direction``, ``Financial_Price`` components and ``Reason`` are
all optional. ``Financial_Price`` components are preserved in ``raw`` but not
interpreted: what they contribute to the published price is not verified.
The A85 imbalance parser was verified against the live API on 2026-10-12
(all 12 zones, compared with eSett and entsoe-py). The parser is deliberately
strict: any deviation from these assumptions raises ``SourceError`` instead of
passing silently.
"""

import html
import io
import lzma
import math
import os
import re
import time
import zipfile
import zlib
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from operator import attrgetter
from types import TracebackType
from typing import Any, Self
from urllib.parse import unquote
from xml.etree import ElementTree
from xml.parsers import expat

import httpx

from nordic_balancing._echo import echoes_key, xml_can_hide_key, xml_echoes_key
from nordic_balancing._http import _LIMIT, _escape, _request, check_base_url
from nordic_balancing.errors import NordicBalancingError, SourceError
from nordic_balancing.models import (
    INTERVAL,
    BiddingZone,
    Direction,
    ImbalancePrice,
    ReserveCapacity,
    ReserveProduct,
    _owned_imbalance_price,
    _owned_reserve_capacity,
    to_utc,
)

SOURCE = "entsoe"
BASE_URL = "https://web-api.tp.entsoe.eu/api"
# Observed request latency did not grow with the window (2026-10-12: 2-42 s for one hour,
# 4-21 s for 31 to 367 days), so ask for a whole (leap) year at a time.
_CHUNK = timedelta(days=366)
_CAPACITY_CHUNK = timedelta(days=7)
# A year of daily documents is a few hundred members; far more is not a response.
_MAX_MEMBERS = 10_000
_INSTANCE_LIMIT = "exceeds the allowed maximum"
_HOUR = timedelta(hours=1)
_NO = (BiddingZone.NO1, BiddingZone.NO2, BiddingZone.NO3, BiddingZone.NO4, BiddingZone.NO5)
_MFRR_DIRECTIONS = {"A01": Direction.UP, "A02": Direction.DOWN}
# Imbalance price categories (excess and insufficient balance) that may share one price.
_MERGEABLE_CATEGORIES = frozenset({"A04", "A05"})
_NO_DATA = "No matching data found"
_PASSTHROUGH = frozenset({400})
_RESOLUTIONS = {"PT15M": INTERVAL, "PT60M": _HOUR}
# The document-level area element: controlArea_Domain.mRID in schema versions up to
# 4.0, area_Domain.mRID from 4.1. Both are optional in every version.
_AREA_ELEMENTS = ("area_Domain.mRID", "controlArea_Domain.mRID")
# The price unit element: price_Measure_Unit.name up to 4.4, price_Measurement_Unit.name
# in 4.5. Both are optional.
_PRICE_UNIT_ELEMENTS = ("price_Measurement_Unit.name", "price_Measure_Unit.name")
# Any version of the IEC 62325-451-6 balancingdocument URN, ASCII digits only.
_BALANCING_NS = re.compile(r"urn:iec62325\.351:tc57wg16:451-6:balancingdocument:[0-9]+:[0-9]+")
# The xs:positiveInteger and xs:decimal lexical forms, ASCII digits only.
_POSITION_TEXT = re.compile(r"\+?[0-9]+")
_DECIMAL_TEXT = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")
# Provider text echoed into an error body may carry the token behind escapes
# the token pattern does not match; _decoded undoes them for the check.
_JSON_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}|\\[\\/]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# The timeInterval lexical form: extended layout with a T separator and the Z
# or +00:00 UTC designator (real documents send HH:MM with no seconds). The
# other zero-offset spellings fromisoformat accepts (-00:00, +00, +0000, offset
# seconds, a space separator, the basic layout) are not valid here.
_TIME_TEXT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?(Z|z|\+00:00)"
)
_EIC_CODES = {
    BiddingZone.DK1: "10YDK-1--------W",
    BiddingZone.DK2: "10YDK-2--------M",
    BiddingZone.FI: "10YFI-1--------U",
    BiddingZone.NO1: "10YNO-1--------2",
    BiddingZone.NO2: "10YNO-2--------T",
    BiddingZone.NO3: "10YNO-3--------J",
    BiddingZone.NO4: "10YNO-4--------9",
    BiddingZone.NO5: "10Y1001A1001A48H",
    BiddingZone.SE1: "10Y1001A1001A44P",
    BiddingZone.SE2: "10Y1001A1001A45N",
    BiddingZone.SE3: "10Y1001A1001A46L",
    BiddingZone.SE4: "10Y1001A1001A47J",
}


class _CapacityInstanceLimit(SourceError):
    """A capacity acknowledgement that permits retrying a smaller time window."""


@dataclass(frozen=True, slots=True)
class _Point:
    start: datetime
    amount: float | None
    categories: tuple[dict[str, float | str | None], ...]
    flow_direction: str | None
    financial_prices: tuple[dict[str, float | str | None], ...]
    resolution: str
    curve_type: str
    namespace: str
    quantity: float | None = None
    capacity_period: tuple[datetime, datetime] | None = None
    capacity_series: object | None = None
    capacity_placeholder: bool = False


class EntsoeClient:
    """Synchronous ENTSO-E client for A85 imbalance prices and A81 FCR-N/mFRR capacity.

    ``api_key`` defaults to ``ENTSOE_API_KEY``. Pass your own ``httpx.Client``
    to control proxies, timeouts or transports; otherwise this instance owns it.
    A request can take tens of seconds, so up to ``max_workers`` requests
    (one per zone and chunk) run in parallel; ``max_workers=1`` runs them in turn.
    """

    def __init__(
        self,
        api_key: str | None = None,
        http: httpx.Client | None = None,
        *,
        base_url: str = BASE_URL,
        max_retries: int = 3,
        max_workers: int = 12,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("ENTSOE_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("api_key or ENTSOE_API_KEY must be set")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        key = key.strip()
        if any(not "!" <= character <= "~" for character in key):
            raise ValueError("api_key must contain only printable ASCII without whitespace")
        self._api_key = key
        check_base_url(base_url, credentialed=True)
        self._owns_http = http is None
        # Observed latency reached 42 s for a single request.
        self._http = http if http is not None else httpx.Client(timeout=120.0)
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._max_workers = max_workers
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

        One document type A85 request is made per zone per chunk of at most
        366 days. ``PT60M`` points are repeated across four quarters, with
        the published resolution retained.
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
        jobs = _jobs(start_utc, end_utc, zone_list)
        workers = min(self._max_workers, len(jobs))
        detail: str | None = None
        try:
            if workers == 1:
                parts = [self._fetch(*job) for job in jobs]
            else:
                pool = ThreadPoolExecutor(workers)
                try:
                    # map yields in job order, so the first failing job raises.
                    parts = list(pool.map(lambda job: self._fetch(*job), jobs))
                finally:
                    pool.shutdown(cancel_futures=True)
        except SourceError as exc:
            # The sanitized error is raised after the handler: inside it the new
            # error would keep the original as __context__, provider text and all.
            detail = _redact(str(exc), self._api_key)
        if detail is not None:
            raise SourceError(detail)
        # Each job covers one zone and a disjoint chunk; overlapping keys
        # indicate invalid chunk results, even when the prices agree.
        collected: dict[tuple[BiddingZone, datetime], ImbalancePrice] = {}
        for part in parts:
            for key, price in part.items():
                if key in collected:
                    raise SourceError(f"{SOURCE}: duplicate price across chunks")
                collected[key] = price
        return sorted(
            (p for p in collected.values() if start_utc <= p.start < end_utc),
            key=attrgetter("start", "zone"),
        )

    def capacity_market(
        self,
        product: ReserveProduct,
        start: datetime,
        end: datetime,
        *,
        zones: Iterable[BiddingZone] = (
            BiddingZone.NO1,
            BiddingZone.NO2,
            BiddingZone.NO3,
            BiddingZone.NO4,
            BiddingZone.NO5,
        ),
    ) -> list[ReserveCapacity]:
        """Return NO FCR-N or NO/FI mFRR capacity, expanded to quarters.

        ENTSO-E does not name the product; FCR-N is inferred from the identical
        query matching EDS FCR-N for Swedish zones and DK2.
        The mFRR product is verified against Fingrid and SvK (2026-10-08).
        Identical quantity/price repeats across documents, members or series
        collapse after A03 filling for both products; differing values raise
        ``SourceError``. Repeated positions within a Period always raise.
        Capacity requests cover at most seven days, halving on the ENTSO-E
        instance-limit acknowledgement down to one hour. For mFRR only, a
        one-Point position-1 zero/zero Period is ignored
        only when another non-placeholder series in the response covers the same
        direction and exact Period.
        """
        if not isinstance(product, ReserveProduct) or product not in (
            ReserveProduct.FCR_N,
            ReserveProduct.MFRR,
        ):
            raise ValueError(
                "ENTSO-E capacity supports ReserveProduct.FCR_N and ReserveProduct.MFRR"
            )
        start_utc = to_utc(start, "start")
        end_utc = to_utc(end, "end")
        if end_utc <= start_utc:
            raise ValueError("end must be after start")
        requested = list(zones)
        supported = (*_NO, BiddingZone.FI) if product is ReserveProduct.MFRR else _NO
        if any(not isinstance(z, BiddingZone) or z not in supported for z in requested):
            if product is ReserveProduct.FCR_N:
                raise ValueError("ENTSO-E FCR-N capacity covers only NO1, NO2, NO3, NO4 and NO5")
            raise ValueError(
                f"ENTSO-E {product.value} capacity covers only "
                + ", ".join(z.value for z in supported)
            )
        zone_list = sorted(set(requested))
        if not zone_list:
            raise ValueError("zones must not be empty")
        jobs = _jobs(start_utc, end_utc, zone_list, max_chunk=_CAPACITY_CHUNK)
        pool = ThreadPoolExecutor(min(self._max_workers, len(jobs)))
        collected: dict[tuple[BiddingZone, datetime, Direction], ReserveCapacity] = {}
        detail: str | None = None
        try:
            try:
                parts = list(
                    pool.map(lambda job: self._fetch_capacity(*job, product=product), jobs)
                )
            finally:
                pool.shutdown(cancel_futures=True)
            for part in parts:
                for record in part:
                    key = (record.zone, record.start, record.direction)
                    existing = collected.get(key)
                    if existing is not None and (
                        existing.volume_mw != record.volume_mw
                        or existing.price_eur_per_mw != record.price_eur_per_mw
                    ):
                        if product is ReserveProduct.FCR_N:
                            raise SourceError(
                                f"{SOURCE}: duplicate capacity for {record.zone} at {record.start}"
                            )
                        raise SourceError(
                            f"{SOURCE}: duplicate capacity with differing values for mFRR "
                            f"{record.zone} at {record.start} ({record.direction.name})"
                        )
                    collected.setdefault(key, record)
        except SourceError as exc:
            # The sanitized error is raised after the handler: inside it the new
            # error would keep the original as __context__, provider text and all.
            detail = _redact(str(exc), self._api_key)
        if detail is not None:
            raise SourceError(detail)
        return sorted(
            (r for r in collected.values() if start_utc <= r.start < end_utc),
            key=attrgetter("start", "zone", "direction"),
        )

    def _fetch_capacity(
        self,
        zone: BiddingZone,
        chunk_start: datetime,
        chunk_end: datetime,
        *,
        product: ReserveProduct = ReserveProduct.FCR_N,
    ) -> list[ReserveCapacity]:
        records: list[ReserveCapacity] = []
        process = "A47" if product is ReserveProduct.MFRR else "A52"
        message: str | None = None
        try:
            points = [
                point
                for member in self._request_members(
                    zone, chunk_start, chunk_end, capacity=True, capacity_process=process
                )
                for point in _points(
                    member,
                    _EIC_CODES[zone],
                    chunk_start,
                    chunk_end,
                    api_key=self._api_key,
                    capacity=True,
                    capacity_process=process,
                )
            ]
        except _CapacityInstanceLimit as exc:
            hours = (chunk_end - chunk_start) // _HOUR
            if hours <= 1:
                # The message is already redacted; it is raised after the
                # handler so the new error keeps no __context__.
                message = str(exc)
            else:
                # Both bounds are whole hours. Round midpoint ties upward, keeping
                # both halves nonempty and reusing per-response whole-day clipping.
                midpoint = chunk_start + ((hours + 1) // 2) * _HOUR
                return self._fetch_capacity(
                    zone, chunk_start, midpoint, product=product
                ) + self._fetch_capacity(zone, midpoint, chunk_end, product=product)
        if message is not None:
            raise SourceError(message)
        if product is ReserveProduct.MFRR:
            # Validate all members before filtering: a placeholder may precede
            # its real counterpart, even in a different member of this response.
            real_series: dict[
                tuple[str | None, tuple[datetime, datetime] | None], set[object | None]
            ] = {}
            for point in points:
                if not point.capacity_placeholder:
                    key = (point.flow_direction, point.capacity_period)
                    real_series.setdefault(key, set()).add(point.capacity_series)
            points = [
                point
                for point in points
                if not point.capacity_placeholder
                or not any(
                    series is not point.capacity_series
                    for series in real_series.get((point.flow_direction, point.capacity_period), ())
                )
            ]
        # Filled values then use the existing product-specific duplicate rules
        # in capacity_market's merge, including repeats within one document.
        for point in points:
            if point.flow_direction is None:
                raise SourceError(f"{SOURCE}: missing flowDirection.direction")
            direction = (
                _MFRR_DIRECTIONS[point.flow_direction]
                if product is ReserveProduct.MFRR
                else Direction.NONE
            )
            point_start = point.start.isoformat()
            for quarter in range(4):
                # point.start is a plain UTC datetime (_time) and each raw is new and flat.
                records.append(
                    _owned_reserve_capacity(
                        start=point.start + quarter * INTERVAL,
                        zone=zone,
                        product=product,
                        direction=direction,
                        price_eur_per_mw=point.amount,
                        volume_mw=point.quantity,
                        source=SOURCE,
                        resolution=_HOUR,
                        raw={
                            "zone": zone.value,
                            "start": point_start,
                            "resolution": point.resolution,
                            "curveType": point.curve_type,
                            "namespace": point.namespace,
                            "quantity": point.quantity,
                            "procurement_Price.amount": point.amount,
                            "flow_direction": point.flow_direction,
                            "businessType": "B95",
                            "currency_Unit.name": "EUR",
                            "quantity_Measure_Unit.name": "MAW",
                        },
                    )
                )
        return records

    def _request_members(
        self,
        zone: BiddingZone,
        chunk_start: datetime,
        chunk_end: datetime,
        *,
        capacity: bool = False,
        capacity_process: str = "A52",
    ) -> list[bytes]:
        params = [
            ("documentType", "A81" if capacity else "A85"),
            ("controlArea_Domain", _EIC_CODES[zone]),
            ("periodStart", _format(chunk_start)),
            ("periodEnd", _format(chunk_end)),
        ]
        if capacity:
            params.extend(
                [
                    ("businessType", "B95"),
                    ("processType", capacity_process),
                    ("Type_MarketAgreement.Type", "A01"),
                ]
            )
        failure: str | None = None
        try:
            response = _request(
                self._http,
                self._base_url,
                "",
                params,
                headers={"SECURITY_TOKEN": self._api_key},
                source=SOURCE,
                max_retries=self._max_retries,
                sleep=self._sleep,
                # Retain the complete error body so redaction precedes truncation.
                passthrough_statuses=frozenset(range(400, 600)),
            )
        except NordicBalancingError:
            # SourceError and RateLimitError carry no URL.
            raise
        except Exception as exc:
            # httpx.HTTPError or any other error from the client or a custom transport:
            # its message or traceback can hold the request URL, and with it the token.
            # A declared class name cannot, so it is kept for diagnosis; a dynamically
            # built name that is not a short identifier is dropped.
            name = type(exc).__name__
            failure = name if name.isidentifier() and len(name) <= 64 else "Exception"
        if failure is not None:
            # Raised after the handler: inside it the new error would keep the
            # original as __context__, request URL and token and all.
            raise SourceError(f"{SOURCE}: request failed ({failure})")
        if response.is_error and response.status_code not in _PASSTHROUGH:
            detail = _redact(response.text, self._api_key)
            message = f"{SOURCE}: HTTP {response.status_code}"
            if detail:
                message += f": {detail[:200]}"
            raise SourceError(message)
        members = _members(response.content)
        if response.status_code != 200 and not all(map(_is_acknowledgement, members)):
            # A 400 is read only for its acknowledgement; data in an error response
            # is not an answer to trust.
            detail = _redact(response.text, self._api_key)[:200]
            raise SourceError(
                f"{SOURCE}: HTTP {response.status_code} without an acknowledgement: {detail}"
            )
        return members

    def _fetch(
        self, zone: BiddingZone, chunk_start: datetime, chunk_end: datetime
    ) -> dict[tuple[BiddingZone, datetime], ImbalancePrice]:
        collected: dict[tuple[BiddingZone, datetime], ImbalancePrice] = {}
        # Each collected record's raw dict, built here and owned by that record alone,
        # so a repeat for the same interval merges into it before anything returns it.
        raws: dict[tuple[BiddingZone, datetime], dict[str, Any]] = {}
        for member in self._request_members(zone, chunk_start, chunk_end):
            for point in _points(
                member, _EIC_CODES[zone], chunk_start, chunk_end, api_key=self._api_key
            ):
                quarters = 4 if point.resolution == "PT60M" else 1
                resolution = _RESOLUTIONS[point.resolution]
                point_start = point.start.isoformat().replace("+00:00", "Z")
                for quarter in range(quarters):
                    interval_start = point.start + quarter * INTERVAL
                    key = (zone, interval_start)
                    existing = collected.get(key)
                    if existing is None:
                        raw: dict[str, Any] = {
                            "zone": zone.value,
                            "start": point_start,
                            "resolution": point.resolution,
                            "curveType": point.curve_type,
                            "namespace": point.namespace,
                            "category": point.categories[0]["category"],
                            "categories": [dict(entry) for entry in point.categories],
                            "amount": point.amount,
                            # flowDirection.direction is kept raw only: its A85
                            # semantics are not verified, so it must not be
                            # mapped to dominating_direction.
                            "flow_direction": point.flow_direction,
                            "financial_prices": [
                                dict(component) for component in point.financial_prices
                            ],
                        }
                        # point.start is a plain UTC datetime (_time) and raw is new.
                        collected[key] = _owned_imbalance_price(
                            start=interval_start,
                            zone=zone,
                            imbalance_price_eur=point.amount,
                            mfrr_up_price_eur=None,
                            mfrr_down_price_eur=None,
                            dominating_direction=None,
                            source=SOURCE,
                            resolution=resolution,
                            raw=raw,
                        )
                        raws[key] = raw
                        continue
                    if existing.resolution != resolution:
                        # Equal values at different resolutions are still two
                        # publications; which one is authoritative is not known.
                        raise SourceError(
                            f"{SOURCE}: conflicting resolutions for {zone.value} at "
                            f"{interval_start.isoformat()}"
                        )
                    merged = raws[key]
                    if existing.imbalance_price_eur != point.amount:
                        found = _category_names(merged["categories"], point.categories)
                        raise SourceError(
                            f"{SOURCE}: dual imbalance prices are not supported "
                            f"(categories: {found})"
                        )
                    _reject_repeated_categories(merged["categories"], point.categories)
                    flow_direction = _flow_direction(merged["flow_direction"], point.flow_direction)
                    merged["categories"].extend(dict(entry) for entry in point.categories)
                    merged["flow_direction"] = flow_direction
                    merged["financial_prices"].extend(
                        dict(component) for component in point.financial_prices
                    )
        return collected


def _jobs(
    start: datetime,
    end: datetime,
    zones: list[BiddingZone],
    *,
    max_chunk: timedelta | None = None,
) -> list[tuple[BiddingZone, datetime, datetime]]:
    query_start = start.replace(minute=0, second=0, microsecond=0)
    query_end = end.replace(minute=0, second=0, microsecond=0)
    if query_end < end:
        query_end += _HOUR
    jobs: list[tuple[BiddingZone, datetime, datetime]] = []
    chunk_start = query_start
    chunk_size = min(_CHUNK, max_chunk) if max_chunk is not None else _CHUNK
    while chunk_start < query_end:
        chunk_end = min(chunk_start + chunk_size, query_end)
        jobs.extend((zone, chunk_start, chunk_end) for zone in zones)
        chunk_start = chunk_end
    return jobs


def _format(value: datetime) -> str:
    return value.strftime("%Y%m%d%H%M")


def _short(value: object) -> str:
    """repr() of a provider value, cut so a huge field stays a short message."""
    text = repr(value)
    return text if len(text) <= 64 else text[:64] + "..."


def _redact(text: str, api_key: str) -> str:
    # An error body echoing the request URL or query carries the token as httpx
    # sent it — percent-encoded — so the raw form alone does not cover it. Each
    # character may appear raw or as its escape in either hex case, so any mix
    # of encoders (slash left unescaped, lowercase hex) is matched too.
    if not api_key:
        return _escape(text)
    redacted = re.sub(_token_pattern(api_key), "[REDACTED_SECRET]", text)
    if _hides_token(redacted, api_key):
        # An encoding the pattern does not match is still the key; none of the
        # provider text is safe to echo.
        return "provider text withheld: it may contain the API token"
    return _escape(redacted)


def _token_pattern(api_key: str) -> str:
    # The constructor allows only printable ASCII, so each character is one byte.
    parts = []
    for char in api_key:
        escaped = "%" + "".join(f"[{d.upper()}{d.lower()}]" for d in f"{ord(char):02x}")
        parts.append(f"(?:{escaped}|{re.escape(char)})")
    return "".join(parts)


def _hides_token(text: str, api_key: str) -> bool:
    """Whether ``text`` still holds the token in an encoding ``_token_pattern`` misses."""
    decoded = _decoded(text)
    key = api_key.lower()
    if key in decoded:
        return True
    # Only the alphanumeric characters are compared, so a spelling like
    # "s.e.c.r.e.t" is caught too; under 16 of them a match could be a
    # coincidence, so the skeleton test is skipped for short keys.
    skeleton = _NON_ALNUM.sub("", key)
    return len(skeleton) >= 16 and skeleton in _NON_ALNUM.sub("", decoded)


def _decoded(text: str) -> str:
    # Up to three rounds, each undoing one layer of every escape a structured
    # error body can wrap the token in: percent-encoding, JSON \/ and \uXXXX
    # escapes and XML character and entity references.
    for _ in range(3):
        decoded = html.unescape(_JSON_ESCAPE.sub(_json_escape, unquote(text)))
        if decoded == text:
            break
        text = decoded
    return text.lower()


def _json_escape(match: re.Match[str]) -> str:
    escape = match.group(0)
    if escape == r"\/":
        return "/"
    if escape == "\\\\":
        return "\\"
    return chr(int(escape[2:], 16))


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


_Groups = dict[str, list[ElementTree.Element]]


def _group(element: ElementTree.Element) -> _Groups:
    """Index an element's children by local name, in document order, in one pass."""
    groups: _Groups = {}
    for child in element:
        name = _local(child.tag)
        found = groups.get(name)
        if found is None:
            groups[name] = [child]
        else:
            found.append(child)
    return groups


def _children(
    element: ElementTree.Element, name: str, groups: _Groups | None = None
) -> list[ElementTree.Element]:
    # groups, when given, is _group(element): the same children without another scan.
    if groups is not None:
        return groups.get(name, [])
    return [child for child in element if _local(child.tag) == name]


def _only_child(
    element: ElementTree.Element, name: str, groups: _Groups | None = None
) -> ElementTree.Element | None:
    children = _children(element, name, groups)
    if len(children) > 1:
        raise SourceError(f"{SOURCE}: more than one {name} in {_local(element.tag)}")
    return children[0] if children else None


def _text(parent: ElementTree.Element, name: str, groups: _Groups | None = None) -> str:
    element = _only_child(parent, name, groups)
    if element is None or element.text is None or not element.text.strip():
        raise SourceError(f"{SOURCE}: missing {name}")
    return element.text.strip()


def _members(body: bytes) -> list[bytes]:
    if len(body) > _LIMIT:
        raise SourceError(f"{SOURCE}: response exceeds {_LIMIT} bytes")
    if not body.startswith(b"PK"):
        members = [body]
    else:
        # Every central-directory record starts with PK\x01\x02: counting them
        # rejects an oversized directory before it is parsed in full.
        if body.count(b"PK\x01\x02") > _MAX_MEMBERS:
            raise SourceError(f"{SOURCE}: ZIP archive has more than {_MAX_MEMBERS} members")
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                entries = archive.infolist()
                # Directory entries count too: each is still parsed from the central directory.
                if len(entries) > _MAX_MEMBERS:
                    raise SourceError(f"{SOURCE}: ZIP archive has more than {_MAX_MEMBERS} members")
                infos = [info for info in entries if not info.is_dir()]
                if not any(info.filename.lower().endswith(".xml") for info in infos):
                    raise SourceError(f"{SOURCE}: ZIP archive has no XML member")
                if sum(info.file_size for info in infos) > _LIMIT:
                    raise SourceError(f"{SOURCE}: ZIP members exceed {_LIMIT} bytes uncompressed")
                # The declared size is only advisory: a member may lie. Each
                # stream is read past the bytes still allowed under _LIMIT, so
                # a lied-about member stops the read at the cap instead of
                # inflating in full before the CRC check fails.
                members = []
                inflated = 0
                for info in infos:
                    with archive.open(info) as stream:
                        member = stream.read(_LIMIT - inflated + 1)
                    inflated += len(member)
                    if inflated > _LIMIT:
                        raise SourceError(
                            f"{SOURCE}: ZIP members exceed {_LIMIT} bytes uncompressed"
                        )
                    members.append(member)
        except (
            zipfile.BadZipFile,
            RuntimeError,
            zlib.error,
            EOFError,
            NotImplementedError,
            ValueError,
            OSError,
            lzma.LZMAError,
        ) as exc:
            raise SourceError(f"{SOURCE}: response is not a valid ZIP archive") from exc
    for member in members:
        # An ENTSO-E document never carries a DTD: one can only declare entities
        # for expansion or external references.
        if _declares_dtd(member):
            raise SourceError(f"{SOURCE}: document has a DTD")
    return members


class _DoctypeFound(Exception):
    pass


def _declares_dtd(member: bytes) -> bool:
    if b"<!DOCTYPE" in member or b"<!ENTITY" in member:
        return True
    if b"\x00" not in member:
        return False
    # UTF-16 or UTF-32, which the parser also accepts: every ASCII character carries
    # a zero byte, so the byte check above cannot see the declaration. These are
    # rare, so the parser itself is asked, stopping at the first declaration.
    parser = expat.ParserCreate()

    def found(*_: object) -> None:
        raise _DoctypeFound

    parser.StartDoctypeDeclHandler = found
    parser.EntityDeclHandler = found
    try:
        parser.Parse(member, True)
    except _DoctypeFound:
        return True
    except (expat.ExpatError, ValueError, LookupError):
        # Unparseable to expat — including a declared utf-16-le/be encoding,
        # which pyexpat rejects with ValueError. The document parse in _points
        # fails on the same input and reports "document is not XML" there.
        return False
    return False


def _is_acknowledgement(member: bytes) -> bool:
    # Decided by the parsed root, so a comment or an encoding cannot pass or fail it.
    try:
        return _local(ElementTree.fromstring(member).tag) == "Acknowledgement_MarketDocument"
    except (ElementTree.ParseError, ValueError, LookupError):
        return False


def _points(
    member: bytes,
    eic: str,
    window_start: datetime,
    window_end: datetime,
    *,
    api_key: str = "",
    capacity: bool = False,
    capacity_process: str = "A52",
) -> list[_Point]:
    try:
        root = ElementTree.fromstring(member)
    except (ElementTree.ParseError, ValueError, LookupError) as exc:
        raise SourceError(f"{SOURCE}: document is not XML") from exc
    name = _local(root.tag)
    if name == "Acknowledgement_MarketDocument":
        _acknowledgement(root, api_key, capacity=capacity)
        return []
    if name != "Balancing_MarketDocument":
        raise SourceError(f"{SOURCE}: unknown root element {_short(name)}")
    namespace = root.tag[1:].partition("}")[0] if root.tag.startswith("{") else ""
    if _BALANCING_NS.fullmatch(namespace) is None:
        raise SourceError(f"{SOURCE}: unsupported namespace {_short(namespace)}")
    groups = _group(root)
    document_type = _text(root, "type", groups)
    if document_type != ("A81" if capacity else "A85"):
        # Other balancing documents (A86 imbalance volumes, ...) carry no prices;
        # parsing one would yield only unpublished intervals.
        raise SourceError(f"{SOURCE}: unexpected document type {_short(document_type)}")
    if capacity and _text(root, "process.processType", groups) != capacity_process:
        raise SourceError(f"{SOURCE}: capacity process.processType must be {capacity_process}")
    for element in _AREA_ELEMENTS:
        area = _code(root, element, groups)
        if area is not None and area != eic:
            raise SourceError(
                f"{SOURCE}: {element} {_short(area)} does not match requested {_short(eic)}"
            )
    points = [
        point
        for series in _children(root, "TimeSeries", groups)
        for point in _series_points(
            series,
            namespace,
            window_start,
            window_end,
            capacity=capacity,
            capacity_process=capacity_process,
        )
    ]
    # A document echoing the credential would carry it into raw and pickles.
    # Checked only once the document read as an answer: one failing an earlier
    # check reports what failed, redacted, like any other response. The decoded
    # scan runs only when the bytes could hide an encoded form; the parsed tree
    # has already undone the character references and the wide encodings.
    if api_key and (
        echoes_key(member, api_key) or (xml_can_hide_key(member) and xml_echoes_key(root, api_key))
    ):
        raise SourceError(f"{SOURCE}: response contains the API token")
    return points


def _acknowledgement(root: ElementTree.Element, api_key: str, *, capacity: bool = False) -> None:
    reason_texts = [
        [(text.text or "").strip() for text in _children(reason, "text")]
        for reason in _children(root, "Reason")
    ]
    instance_limit = capacity and any(
        _INSTANCE_LIMIT in text for texts in reason_texts for text in texts
    )
    if (
        not instance_limit
        and reason_texts
        and all(texts and all(_NO_DATA in text for text in texts) for texts in reason_texts)
    ):
        return
    reasons = ["; ".join(texts) for texts in reason_texts]
    detail = "; ".join(reason for reason in reasons if reason)
    if not detail:
        raise SourceError(f"{SOURCE}: acknowledgement without a reason")
    if api_key:
        detail = _redact(detail, api_key)
    message = f"{SOURCE}: acknowledgement: {detail[:200]}"
    if instance_limit:
        raise _CapacityInstanceLimit(message)
    raise SourceError(message)


def _series_points(
    series: ElementTree.Element,
    namespace: str,
    window_start: datetime,
    window_end: datetime,
    *,
    capacity: bool = False,
    capacity_process: str = "A52",
) -> list[_Point]:
    groups = _group(series)
    series_flow: str | None = None
    if capacity:
        required = [
            ("businessType", "B95"),
            ("quantity_Measure_Unit.name", "MAW"),
            ("currency_Unit.name", "EUR"),
        ]
        if capacity_process == "A52":
            # Preserve FCR-N's validation order and exact errors from main.
            required.insert(1, ("flowDirection.direction", "A03"))
        for name, expected in required:
            if _text(series, name, groups) != expected:
                raise SourceError(f"{SOURCE}: capacity {name} must be {expected}")
        series_flow = _text(series, "flowDirection.direction", groups)
        if capacity_process == "A47" and series_flow not in ("A01", "A02"):
            raise SourceError(f"{SOURCE}: mFRR capacity flowDirection.direction must be A01 or A02")
    currency = _only_child(series, "currency_Unit.name", groups)
    if currency is not None and (currency.text or "").strip() != "EUR":
        raise SourceError(
            f"{SOURCE}: unsupported currency_Unit.name {_short((currency.text or '').strip())}"
        )
    for element in _PRICE_UNIT_ELEMENTS:
        unit = _only_child(series, element, groups)
        expected_unit = "MAW" if capacity else "MWH"
        if unit is not None and (unit.text or "").strip() != expected_unit:
            raise SourceError(
                f"{SOURCE}: unsupported {element} {_short((unit.text or '').strip())}"
            )
    curve_type = _text(series, "curveType", groups)
    # A02, A04 and A05 have different semantics (instants or variable-size
    # blocks, not a fixed-resolution series) and must not be guessed.
    if curve_type not in ("A01", "A03"):
        raise SourceError(f"{SOURCE}: curveType {curve_type} is not supported")
    series_id = object() if capacity else None
    return [
        point
        for period in _children(series, "Period", groups)
        for point in _period_points(
            period,
            curve_type,
            namespace,
            window_start,
            window_end,
            capacity=capacity,
            capacity_flow=series_flow,
            capacity_series=series_id,
        )
    ]


def _period_points(
    period: ElementTree.Element,
    curve_type: str,
    namespace: str,
    window_start: datetime,
    window_end: datetime,
    *,
    capacity: bool = False,
    capacity_flow: str | None = None,
    capacity_series: object | None = None,
) -> list[_Point]:
    groups = _group(period)
    interval = _only_child(period, "timeInterval", groups)
    if interval is None:
        raise SourceError(f"{SOURCE}: Period has no timeInterval")
    period_start = _time(_text(interval, "start"))
    period_end = _time(_text(interval, "end"))
    resolution = _text(period, "resolution", groups)
    if capacity and resolution != "PT60M":
        raise SourceError(f"{SOURCE}: capacity resolution must be PT60M")
    if resolution not in _RESOLUTIONS:
        raise SourceError(f"{SOURCE}: unsupported resolution {_short(resolution)}")
    if period_end <= period_start:
        raise SourceError(f"{SOURCE}: Period end is not after its start")
    step = _RESOLUTIONS[resolution]
    if (period_end - period_start) % step:
        # A partial last step would be expanded past the Period end.
        raise SourceError(f"{SOURCE}: Period is not a whole number of {resolution}")
    count = (period_end - period_start) // step
    if capacity and period_end - period_start > _CAPACITY_CHUNK + timedelta(days=1):
        # A81 Periods are delivery days inside a 7-day request; one far longer is
        # not an answer to it, and filling it hour by hour would not terminate soon.
        raise SourceError(f"{SOURCE}: capacity Period is longer than the request")
    explicit: dict[int, _Point] = {}
    for element in _children(period, "Point", groups):
        fields = _group(element)
        position = _position(_text(element, "position", fields))
        if position > count:
            raise SourceError(f"{SOURCE}: position {position} beyond Period end")
        start = period_start + (position - 1) * step
        if start.minute % 15 or start.second or start.microsecond:
            raise SourceError(f"{SOURCE}: point is not on a 15-minute boundary")
        if resolution == "PT60M" and start.minute:
            raise SourceError(f"{SOURCE}: hourly point is not on the hour")
        if not capacity and not window_start <= start < window_end:
            raise SourceError(f"{SOURCE}: point outside requested window")
        amount_name = "procurement_Price.amount" if capacity else "imbalance_Price.amount"
        amount: float | None
        if capacity:
            amount = _amount(_text(element, amount_name, fields), amount_name)
        else:
            amount_element = _only_child(element, amount_name, fields)
            amount = (
                _amount((amount_element.text or "").strip(), amount_name)
                if amount_element is not None
                else None
            )
        if capacity:
            point_flow = _code(element, "flowDirection.direction", fields)
            if point_flow is not None and point_flow != capacity_flow:
                if capacity_flow == "A03":
                    raise SourceError(f"{SOURCE}: capacity flowDirection.direction must be A03")
                raise SourceError(
                    f"{SOURCE}: mFRR capacity flowDirection.direction conflicts with series"
                )
        record = _Point(
            start=start,
            amount=amount,
            categories=(
                {"category": _code(element, "imbalance_Price.category", fields), "amount": amount},
            ),
            flow_direction=capacity_flow
            if capacity
            else _code(element, "flowDirection.direction", fields),
            financial_prices=tuple(
                _financial_price(component)
                for component in _children(element, "Financial_Price", fields)
            ),
            resolution=resolution,
            curve_type=curve_type,
            namespace=namespace,
            quantity=_amount(_text(element, "quantity", fields), "quantity") if capacity else None,
        )
        existing = explicit.get(position)
        if existing is None:
            explicit[position] = record
            continue
        if capacity:
            raise SourceError(f"{SOURCE}: duplicate capacity position {position} in Period")
        if existing.amount != record.amount:
            raise SourceError(
                f"{SOURCE}: dual imbalance prices are not supported "
                f"(categories: {_category_names(existing.categories, record.categories)})"
            )
        _reject_repeated_categories(existing.categories, record.categories)
        # Category publications must use separate series. Combining them
        # before A03 filling would lose each category's omitted positions.
        raise SourceError(f"{SOURCE}: duplicate position {position} in Period")
    first = explicit.get(1)
    placeholder = (
        capacity
        and capacity_flow in ("A01", "A02")
        and len(explicit) == 1
        and first is not None
        and first.quantity == 0
        and first.amount == 0
    )
    points: list[_Point] = []
    previous: _Point | None = None
    capacity_period = (period_start, period_end) if capacity else None
    for index in range(1, count + 1):
        start = period_start + (index - 1) * step
        # A81 returns whole delivery days: validate and fill the whole Period
        # before clipping to the chunk. A85 retains its window-bounded filling.
        if not capacity and curve_type == "A03" and start >= window_end:
            break
        current = explicit.get(index)
        if current is None:
            if curve_type == "A01" or previous is None:
                raise SourceError(f"{SOURCE}: missing position {index} in Period")
            if not capacity and start < window_start:
                continue
            current = previous
        else:
            previous = current
        if not capacity or window_start <= start < window_end:
            # Positional, in field order: cheaper than dataclasses.replace in profiles.
            points.append(
                _Point(
                    start,
                    current.amount,
                    current.categories,
                    current.flow_direction,
                    current.financial_prices,
                    current.resolution,
                    current.curve_type,
                    current.namespace,
                    current.quantity,
                    capacity_period,
                    capacity_series,
                    placeholder,
                )
            )
    return points


def _code(parent: ElementTree.Element, name: str, groups: _Groups | None = None) -> str | None:
    element = _only_child(parent, name, groups)
    if element is None:
        return None
    return _text(parent, name, groups)


def _financial_price(element: ElementTree.Element) -> dict[str, float | str | None]:
    return {
        "amount": _amount(_text(element, "amount"), "Financial_Price.amount"),
        "direction": _code(element, "direction"),
        "price_descriptor": _code(element, "priceDescriptor.type"),
    }


def _flow_direction(first: str | None, second: str | None) -> str | None:
    if first is not None and second is not None and first != second:
        raise SourceError(
            f"{SOURCE}: conflicting flowDirection.direction {_short(first)} and {_short(second)}"
        )
    return first if first is not None else second


def _category_names(*groups: Iterable[dict[str, Any]]) -> str:
    found = sorted(
        {entry["category"] for group in groups for entry in group if entry["category"] is not None}
    )
    return ", ".join(found) if found else "none"


def _reject_repeated_categories(
    existing: Iterable[dict[str, Any]], incoming: Iterable[dict[str, Any]]
) -> None:
    # Equal prices merge only across distinct categories (A04 and A05); the same
    # category twice, or a point without one beside any other, is a duplicate
    # record (contract C3): an absent category is not a category of its own.
    existing_names = {entry["category"] for entry in existing}
    incoming_names = {entry["category"] for entry in incoming}
    repeated = existing_names & incoming_names
    if None in existing_names | incoming_names:
        repeated.add(None)
    if repeated:
        names = ", ".join(sorted(name or "none" for name in repeated))
        raise SourceError(f"{SOURCE}: duplicate price for category {names}")
    # Every Nordic A85 duplicate seen live is the A04/A05 pair; another
    # category is not known to carry the same price, so it is not merged.
    unknown = sorted((existing_names | incoming_names) - _MERGEABLE_CATEGORIES)
    if unknown:
        names = ", ".join(_short(name) for name in unknown)
        raise SourceError(f"{SOURCE}: equal prices in unsupported categories {names}")


def _position(text: str) -> int:
    if _POSITION_TEXT.fullmatch(text) is None:
        raise SourceError(f"{SOURCE}: unparseable position {_short(text)}")
    try:
        position = int(text, 10)
    except ValueError as exc:
        # A digit string past the interpreter's conversion limit still fails here.
        raise SourceError(f"{SOURCE}: unparseable position {_short(text)}") from exc
    if position < 1:
        raise SourceError(f"{SOURCE}: position {position} is not 1-based")
    return position


def _amount(text: str, name: str) -> float:
    try:
        amount = float(text)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable {name} {_short(text)}") from exc
    if not math.isfinite(amount):
        raise SourceError(f"{SOURCE}: {name} is not finite: {_short(text)}")
    if _DECIMAL_TEXT.fullmatch(text) is None:
        raise SourceError(f"{SOURCE}: unparseable {name} {_short(text)}")
    return amount


def _time(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable timeInterval time {_short(text)}") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or _TIME_TEXT.fullmatch(text) is None
    ):
        raise SourceError(f"{SOURCE}: timeInterval time must be UTC: {_short(text)}")
    return parsed.astimezone(UTC)
