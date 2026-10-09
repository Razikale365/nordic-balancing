"""ENTSO-E Transparency Platform imbalance prices (document type A85).

The XML structure handled here was verified against the official IEC
62325-451-6 balancing document schema, namespace versions 3.0 to 4.5, using
the XSDs published in ENTSO-E's EDI library (CIM_xsd_package_v2026) and the
generated models in entsoe-apy. In that schema a ``Point`` requires only
``position``; ``imbalance_Price.amount``, ``imbalance_Price.category``,
``flowDirection.direction``, ``Financial_Price`` components and ``Reason`` are
all optional. ``Financial_Price`` components are preserved in ``raw`` but not
interpreted: what they contribute to the published price is not verified.
It has NOT been verified against the live API yet, because no API token is
available. The parser is deliberately strict: any deviation from these
assumptions raises ``SourceError`` instead of passing silently.
"""

import io
import math
import os
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from xml.etree import ElementTree

import httpx

from nordic_balancing._http import get_bytes
from nordic_balancing.errors import SourceError
from nordic_balancing.models import INTERVAL, BiddingZone, ImbalancePrice, to_utc

SOURCE = "entsoe"
BASE_URL = "https://web-api.tp.entsoe.eu/api"
_CHUNK = timedelta(days=7)
_HOUR = timedelta(hours=1)
_LIMIT = 64 * 1024 * 1024
_NO_DATA = "No matching data found"
_PASSTHROUGH = frozenset({400})
_RESOLUTIONS = {"PT15M": INTERVAL, "PT60M": _HOUR}
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


class EntsoeClient:
    """Synchronous ENTSO-E Transparency Platform client for A85 imbalance prices.

    ``api_key`` defaults to ``ENTSOE_API_KEY``. Pass your own ``httpx.Client``
    to control proxies, timeouts or transports; otherwise this instance owns it.
    """

    def __init__(
        self,
        api_key: str | None = None,
        http: httpx.Client | None = None,
        *,
        base_url: str = BASE_URL,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("ENTSOE_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("api_key or ENTSOE_API_KEY must be set")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self._api_key = key
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

        One document type A85 request is made per zone per chunk of at most
        seven days. ``PT60M`` points are repeated across four quarters, with
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
        query_start = start_utc.replace(minute=0, second=0, microsecond=0)
        query_end = end_utc.replace(minute=0, second=0, microsecond=0)
        if query_end < end_utc:
            query_end += _HOUR
        collected: dict[tuple[BiddingZone, datetime], ImbalancePrice] = {}
        try:
            chunk_start = query_start
            while chunk_start < query_end:
                chunk_end = min(chunk_start + _CHUNK, query_end)
                for zone in zone_list:
                    self._collect(collected, zone, chunk_start, chunk_end)
                chunk_start = chunk_end
        except SourceError as exc:
            raise SourceError(str(exc).replace(self._api_key, "[REDACTED_SECRET]")) from None
        return sorted(
            (p for p in collected.values() if start_utc <= p.start < end_utc),
            key=lambda p: (p.start, p.zone),
        )

    def _collect(
        self,
        collected: dict[tuple[BiddingZone, datetime], ImbalancePrice],
        zone: BiddingZone,
        chunk_start: datetime,
        chunk_end: datetime,
    ) -> None:
        params = [
            ("securityToken", self._api_key),
            ("documentType", "A85"),
            ("controlArea_Domain", _EIC_CODES[zone]),
            ("periodStart", _format(chunk_start)),
            ("periodEnd", _format(chunk_end)),
        ]
        try:
            body, _ = get_bytes(
                self._http,
                self._base_url,
                "",
                params,
                source=SOURCE,
                max_retries=self._max_retries,
                sleep=self._sleep,
                passthrough_statuses=_PASSTHROUGH,
            )
        except httpx.HTTPError:
            raise SourceError(f"{SOURCE}: request failed") from None
        for member in _members(body):
            for point in _points(member, chunk_start, chunk_end):
                quarters = 4 if point.resolution == "PT60M" else 1
                for quarter in range(quarters):
                    interval_start = point.start + quarter * INTERVAL
                    price = ImbalancePrice(
                        start=interval_start,
                        zone=zone,
                        imbalance_price_eur=point.amount,
                        imbalance_price_dkk=None,
                        spot_price_eur=None,
                        dominating_direction=None,
                        satisfied_demand_mw=None,
                        afrr_up_vwa_eur=None,
                        afrr_down_vwa_eur=None,
                        mfrr_up_price_eur=None,
                        mfrr_down_price_eur=None,
                        source=SOURCE,
                        resolution=_RESOLUTIONS[point.resolution],
                        raw={
                            "zone": zone.value,
                            "start": point.start.isoformat().replace("+00:00", "Z"),
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
                        },
                    )
                    key = (zone, interval_start)
                    existing = collected.get(key)
                    if existing is None:
                        collected[key] = price
                        continue
                    if existing.imbalance_price_eur != price.imbalance_price_eur:
                        found = _category_names(existing.raw["categories"], price.raw["categories"])
                        raise SourceError(
                            f"{SOURCE}: dual imbalance prices are not supported "
                            f"(categories: {found})"
                        )
                    merged: dict[str, Any] = dict(existing.raw)
                    merged["categories"] = [
                        *existing.raw["categories"],
                        *price.raw["categories"],
                    ]
                    merged["flow_direction"] = _flow_direction(
                        existing.raw["flow_direction"], price.raw["flow_direction"]
                    )
                    merged["financial_prices"] = [
                        *existing.raw["financial_prices"],
                        *price.raw["financial_prices"],
                    ]
                    collected[key] = replace(existing, raw=merged)


def _format(value: datetime) -> str:
    return value.strftime("%Y%m%d%H%M")


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local(child.tag) == name]


def _child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(parent: ElementTree.Element, name: str) -> str:
    element = _child(parent, name)
    if element is None or element.text is None or not element.text.strip():
        raise SourceError(f"{SOURCE}: missing {name}")
    return element.text.strip()


def _members(body: bytes) -> list[bytes]:
    if len(body) > _LIMIT:
        raise SourceError(f"{SOURCE}: response exceeds {_LIMIT} bytes")
    if not body.startswith(b"PK"):
        return [body]
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            if sum(info.file_size for info in members) > _LIMIT:
                raise SourceError(f"{SOURCE}: ZIP members exceed {_LIMIT} bytes uncompressed")
            return [archive.read(info) for info in members]
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise SourceError(f"{SOURCE}: response is not a valid ZIP archive") from exc


def _points(member: bytes, window_start: datetime, window_end: datetime) -> list[_Point]:
    try:
        root = ElementTree.fromstring(member)
    except ElementTree.ParseError as exc:
        raise SourceError(f"{SOURCE}: document is not XML") from exc
    name = _local(root.tag)
    if name == "Acknowledgement_MarketDocument":
        _acknowledgement(root)
        return []
    if name != "Balancing_MarketDocument":
        raise SourceError(f"{SOURCE}: unknown root element {name!r}")
    namespace = root.tag[1:].partition("}")[0] if root.tag.startswith("{") else ""
    if "451-6:balancingdocument" not in namespace:
        raise SourceError(f"{SOURCE}: unsupported namespace {namespace!r}")
    return [
        point
        for series in _children(root, "TimeSeries")
        for point in _series_points(series, namespace, window_start, window_end)
    ]


def _acknowledgement(root: ElementTree.Element) -> None:
    reasons = [
        (text.text or "").strip()
        for reason in _children(root, "Reason")
        for text in _children(reason, "text")
    ]
    if any(_NO_DATA in reason for reason in reasons):
        return
    detail = "; ".join(reason for reason in reasons if reason)
    if not detail:
        raise SourceError(f"{SOURCE}: acknowledgement without a reason")
    raise SourceError(f"{SOURCE}: acknowledgement: {detail[:200]}")


def _series_points(
    series: ElementTree.Element,
    namespace: str,
    window_start: datetime,
    window_end: datetime,
) -> list[_Point]:
    currency = _child(series, "currency_Unit.name")
    if currency is not None and (currency.text or "").strip() != "EUR":
        raise SourceError(
            f"{SOURCE}: unsupported currency_Unit.name {(currency.text or '').strip()!r}"
        )
    unit = _child(series, "price_Measurement_Unit.name")
    if unit is not None and (unit.text or "").strip() != "MWH":
        raise SourceError(
            f"{SOURCE}: unsupported price_Measurement_Unit.name {(unit.text or '').strip()!r}"
        )
    curve_type = _text(series, "curveType")
    # A02, A04 and A05 have different semantics (instants or variable-size
    # blocks, not a fixed-resolution series) and must not be guessed.
    if curve_type not in ("A01", "A03"):
        raise SourceError(f"{SOURCE}: curveType {curve_type} is not supported")
    return [
        point
        for period in _children(series, "Period")
        for point in _period_points(period, curve_type, namespace, window_start, window_end)
    ]


def _period_points(
    period: ElementTree.Element,
    curve_type: str,
    namespace: str,
    window_start: datetime,
    window_end: datetime,
) -> list[_Point]:
    interval = _child(period, "timeInterval")
    if interval is None:
        raise SourceError(f"{SOURCE}: Period has no timeInterval")
    period_start = _time(_text(interval, "start"))
    period_end = _time(_text(interval, "end"))
    resolution = _text(period, "resolution")
    if resolution not in _RESOLUTIONS:
        raise SourceError(f"{SOURCE}: unsupported resolution {resolution!r}")
    if period_end <= period_start:
        raise SourceError(f"{SOURCE}: Period end is not after its start")
    step = _RESOLUTIONS[resolution]
    count = math.ceil((period_end - period_start) / step)
    explicit: dict[int, _Point] = {}
    for element in _children(period, "Point"):
        position = _position(_text(element, "position"))
        if position > count:
            raise SourceError(f"{SOURCE}: position {position} beyond Period end")
        start = period_start + (position - 1) * step
        if start.minute % 15 or start.second or start.microsecond:
            raise SourceError(f"{SOURCE}: point is not on a 15-minute boundary")
        if not window_start <= start < window_end:
            raise SourceError(f"{SOURCE}: point outside requested window")
        amount_element = _child(element, "imbalance_Price.amount")
        amount = (
            _amount((amount_element.text or "").strip(), "imbalance_Price.amount")
            if amount_element is not None
            else None
        )
        record = _Point(
            start=start,
            amount=amount,
            categories=(
                {"category": _code(element, "imbalance_Price.category"), "amount": amount},
            ),
            flow_direction=_code(element, "flowDirection.direction"),
            financial_prices=tuple(
                _financial_price(component) for component in _children(element, "Financial_Price")
            ),
            resolution=resolution,
            curve_type=curve_type,
            namespace=namespace,
        )
        existing = explicit.get(position)
        if existing is None:
            explicit[position] = record
            continue
        if existing.amount != record.amount:
            raise SourceError(
                f"{SOURCE}: dual imbalance prices are not supported "
                f"(categories: {_category_names(existing.categories, record.categories)})"
            )
        explicit[position] = replace(
            existing,
            categories=existing.categories + record.categories,
            flow_direction=_flow_direction(existing.flow_direction, record.flow_direction),
            financial_prices=existing.financial_prices + record.financial_prices,
        )
    points: list[_Point] = []
    previous: _Point | None = None
    for index in range(1, count + 1):
        current = explicit.get(index)
        if current is None:
            if curve_type == "A01" or previous is None:
                raise SourceError(f"{SOURCE}: missing position {index} in Period")
            current = previous
        else:
            previous = current
        start = period_start + (index - 1) * step
        points.append(replace(current, start=start))
    return points


def _code(parent: ElementTree.Element, name: str) -> str | None:
    element = _child(parent, name)
    if element is None:
        return None
    return (element.text or "").strip() or None


def _financial_price(element: ElementTree.Element) -> dict[str, float | str | None]:
    return {
        "amount": _amount(_text(element, "amount"), "Financial_Price.amount"),
        "direction": _code(element, "direction"),
        "price_descriptor": _code(element, "priceDescriptor.type"),
    }


def _flow_direction(first: str | None, second: str | None) -> str | None:
    if first is not None and second is not None and first != second:
        raise SourceError(f"{SOURCE}: conflicting flowDirection.direction {first!r} and {second!r}")
    return first if first is not None else second


def _category_names(*groups: Iterable[dict[str, Any]]) -> str:
    found = sorted(
        {entry["category"] for group in groups for entry in group if entry["category"] is not None}
    )
    return ", ".join(found) if found else "none"


def _position(text: str) -> int:
    try:
        position = int(text, 10)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable position {text!r}") from exc
    if position < 1:
        raise SourceError(f"{SOURCE}: position {position} is not 1-based")
    return position


def _amount(text: str, name: str) -> float:
    try:
        amount = float(text)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable {name} {text!r}") from exc
    if not math.isfinite(amount):
        raise SourceError(f"{SOURCE}: {name} is not finite: {text!r}")
    return amount


def _time(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SourceError(f"{SOURCE}: unparseable timeInterval time {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SourceError(f"{SOURCE}: timeInterval time must be UTC: {text!r}")
    return parsed.astimezone(UTC)
