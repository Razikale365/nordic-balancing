"""Property-based and metamorphic tests for time windows, chunking and source rows.

In the window-splitting properties each fake server is a deterministic function
of the request: it honours the request window exactly as the client sends it and
returns only records whose start lies in ``[start, end)`` (exclusive end), with
prices determined by zone and start. The source-response properties instead
generate ENTSO-E Periods (including invalid Periods that must raise
``SourceError``) and sparse hourly rows with random values. No test touches the
network.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    DivergenceKind,
    EnergiDataServiceClient,
    EntsoeClient,
    ESettClient,
    FingridClient,
    ImbalancePrice,
    ReserveProduct,
    SourceError,
    SvKClient,
    reconcile_imbalance_prices,
)
from nordic_balancing.sources import energidataservice, entsoe, fingrid
from nordic_balancing.sources.entsoe import _EIC_CODES as _ENTSOE_EIC
from nordic_balancing.sources.esett import _EIC_CODES as _ESETT_EIC

Handler = Callable[[httpx.Request], httpx.Response]
HOUR = timedelta(hours=1)
ESETT_SWITCH = datetime(2023, 5, 21, 22, tzinfo=UTC)
FINGRID_SWITCH = datetime(2025, 3, 18, 23, tzinfo=UTC)
FINGRID_QUARTER_ERA = datetime(2025, 3, 14, 23, 15, tzinfo=UTC)
NS_45 = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:5"

_ESETT_ZONE = {code: zone for zone, code in _ESETT_EIC.items()}
_ENTSOE_ZONE = {code: zone for zone, code in _ENTSOE_EIC.items()}
_ZONES = tuple(BiddingZone)


def _us(delta: timedelta) -> int:
    return delta // timedelta(microseconds=1)


def _ceil_quarter(stamp: datetime) -> datetime:
    floor = stamp.replace(minute=stamp.minute // 15 * 15, second=0, microsecond=0)
    return floor if floor == stamp else floor + INTERVAL


def _floor_hour(stamp: datetime) -> datetime:
    return stamp.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(stamp: datetime) -> datetime:
    floor = _floor_hour(stamp)
    return floor if floor == stamp else floor + HOUR


def _grid(start: datetime, end: datetime) -> list[datetime]:
    stamp = _ceil_quarter(start)
    stamps: list[datetime] = []
    while stamp < end:
        stamps.append(stamp)
        stamp += INTERVAL
    return stamps


def _iso_z(stamp: datetime) -> str:
    return stamp.isoformat().replace("+00:00", "Z")


def _zone_index(zone: BiddingZone) -> int:
    return _ZONES.index(zone)


# --- strategies ---------------------------------------------------------------


@st.composite
def _window(
    draw: st.DrawFn,
    earliest: datetime,
    latest: datetime,
    *,
    crossing: datetime | None = None,
    end_limit: datetime | None = None,
    min_step: timedelta | None = None,
) -> tuple[datetime, datetime]:
    """Anchor + offset of 0-40 days (whole seconds plus microseconds) and a
    length of 1 minute to 20 days. ``crossing`` forces start < it < end;
    ``end_limit`` caps the window end; ``min_step`` (INTERVAL or HOUR) forces
    the window to contain a step boundary, so the response is never empty."""
    anchor = draw(
        st.datetimes(
            min_value=earliest.replace(tzinfo=None),
            max_value=latest.replace(tzinfo=None),
            timezones=st.just(UTC),
        )
    )
    offset_cap = _us(timedelta(days=40))
    if crossing is not None:
        offset_cap = min(offset_cap, _us(crossing - anchor) - 1)
    if end_limit is not None:
        reserve = min_step or timedelta(minutes=1)
        offset_cap = min(offset_cap, _us(end_limit - anchor - reserve))
    offset = timedelta(
        seconds=draw(st.integers(0, offset_cap // 1_000_000)),
        microseconds=draw(st.integers(0, 999_999)),
    )
    if _us(offset) > offset_cap:
        offset = timedelta(microseconds=offset_cap)
    start = anchor + offset
    min_length = _us(timedelta(minutes=1))
    if crossing is not None:
        min_length = max(min_length, _us(crossing - start) + 1)
    if min_step is not None:
        first = _ceil_quarter(start) if min_step == INTERVAL else _ceil_hour(start)
        if first > start:
            min_length = max(min_length, _us(first - start) + 1)
    max_length = _us(timedelta(days=20))
    if end_limit is not None:
        max_length = min(max_length, _us(end_limit - start))
    return start, start + timedelta(microseconds=draw(st.integers(min_length, max_length)))


def _zones(members: tuple[BiddingZone, ...], max_size: int) -> st.SearchStrategy[list[BiddingZone]]:
    return st.lists(st.sampled_from(members), min_size=1, max_size=max_size, unique=True)


@st.composite
def _case(
    draw: st.DrawFn,
    earliest: datetime,
    latest: datetime,
    zones: st.SearchStrategy[list[BiddingZone]],
    *,
    crossing: datetime | None = None,
    end_limit: datetime | None = None,
    min_step: timedelta | None = None,
) -> tuple[datetime, datetime, datetime, list[BiddingZone]]:
    """A window [start, end), a split point strictly inside, and the zones."""
    start, end = draw(
        _window(earliest, latest, crossing=crossing, end_limit=end_limit, min_step=min_step)
    )
    middle = start + timedelta(microseconds=draw(st.integers(1, _us(end - start) - 1)))
    return start, middle, end, draw(zones)


# --- shared assertions --------------------------------------------------------


def _assert_grid(prices: list[ImbalancePrice], start: datetime, end: datetime) -> None:
    for price in prices:
        assert price.start.utcoffset() == timedelta(0)
        assert price.start.minute % 15 == 0
        assert price.start.second == 0 and price.start.microsecond == 0
        assert start <= price.start < end


def _assert_price_invariants(
    prices: list[ImbalancePrice], zones: list[BiddingZone], start: datetime, end: datetime
) -> None:
    _assert_grid(prices, start, end)
    keys = [(price.start, price.zone) for price in prices]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    expected = _grid(start, end)
    for zone in set(zones):
        assert sorted(p.start for p in prices if p.zone == zone) == expected


def _price_tuples(
    prices: list[ImbalancePrice],
) -> list[tuple[datetime, BiddingZone, float | None, timedelta]]:
    return [(p.start, p.zone, p.imbalance_price_eur, p.resolution) for p in prices]


# --- eSett --------------------------------------------------------------------


def _esett_value(zone: BiddingZone, stamp: datetime) -> float:
    return float(int(stamp.timestamp()) // 900 % 997) + _zone_index(zone) / 100


def _esett_record(zone: BiddingZone, stamp: datetime) -> dict[str, Any]:
    value = _esett_value(zone, stamp)
    return {
        "timestampUTC": _iso_z(stamp),
        "mba": zone.value,
        "imblSalesPrice": value,
        "imblPurchasePrice": value,
        "upRegPrice": None,
        "downRegPrice": None,
        "mainDirRegPowerPerMBA": None,
    }


def _esett_handler(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    start = datetime.fromisoformat(params["start"])
    end = datetime.fromisoformat(params["end"])
    records = []
    for code in params.get_list("mba"):
        zone = _ESETT_ZONE[code]
        stamp = _ceil_quarter(start)
        while stamp < end:
            if stamp < ESETT_SWITCH:
                if stamp.minute == 0:
                    records.append(_esett_record(zone, stamp))
            else:
                records.append(_esett_record(zone, stamp))
            stamp += INTERVAL
    return httpx.Response(200, json=records)


@given(
    case=_case(
        datetime(2023, 5, 2, tzinfo=UTC),
        datetime(2023, 5, 21, 20, tzinfo=UTC),
        _zones(_ZONES, 3),
        crossing=ESETT_SWITCH,
    )
)
@settings(max_examples=30, deadline=None)
def test_esett_same_records_however_the_window_is_split(
    case: tuple[datetime, datetime, datetime, list[BiddingZone]],
) -> None:
    start, middle, end, zones = case
    client = ESettClient(
        httpx.Client(transport=httpx.MockTransport(_esett_handler)), sleep=lambda _: None
    )
    whole = client.imbalance_prices(start, end, zones)
    _assert_price_invariants(whole, zones, start, end)
    left = client.imbalance_prices(start, middle, zones)
    right = client.imbalance_prices(middle, end, zones)
    assert _price_tuples(left + right) == _price_tuples(whole)
    for price in whole:
        stamp = price.start if price.resolution == INTERVAL else _floor_hour(price.start)
        assert price.imbalance_price_eur == _esett_value(price.zone, stamp)
        assert (price.resolution == HOUR) == (price.start < ESETT_SWITCH)


# --- Energi Data Service -------------------------------------------------------


def _eds_value(zone: BiddingZone, stamp: datetime) -> float:
    return float(int(stamp.timestamp()) // 900 % 997) + _zone_index(zone) / 100


def _eds_handler(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    start = datetime.strptime(params["start"], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    end = datetime.strptime(params["end"], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    zones = sorted(BiddingZone[name] for name in json.loads(params["filter"])["PriceArea"])
    records = []
    for stamp in _grid(start, end):
        for zone in zones:
            value = _eds_value(zone, stamp)
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
    return httpx.Response(
        200, json={"total": len(records), "records": records[offset : offset + limit]}
    )


@given(
    case=_case(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 3, 1, tzinfo=UTC),
        _zones((BiddingZone.DK1, BiddingZone.DK2), 2),
        end_limit=datetime(2026, 4, 1, tzinfo=UTC),
        min_step=INTERVAL,
    )
)
@settings(max_examples=30, deadline=None)
def test_energidataservice_same_records_however_the_window_is_split(
    case: tuple[datetime, datetime, datetime, list[BiddingZone]],
) -> None:
    start, middle, end, zones = case
    client = EnergiDataServiceClient(
        httpx.Client(transport=httpx.MockTransport(_eds_handler)), sleep=lambda _: None
    )
    with pytest.MonkeyPatch().context() as patch:
        patch.setattr(energidataservice, "PAGE_SIZE", 97)
        whole = client.imbalance_prices(start, end, zones)
        _assert_price_invariants(whole, zones, start, end)
        left = client.imbalance_prices(start, middle, zones)
        right = client.imbalance_prices(middle, end, zones)
        assert _price_tuples(left + right) == _price_tuples(whole)
        for price in whole:
            assert price.resolution == INTERVAL
            assert price.imbalance_price_eur == _eds_value(price.zone, price.start)


# --- Fingrid -------------------------------------------------------------------


def _fingrid_value(dataset: int, stamp: datetime, hourly: bool) -> float:
    seed = int(stamp.timestamp()) // (3600 if hourly else 900) + dataset
    return float(seed % 997) + (1000.0 if hourly else 0.0)


def _fingrid_row(dataset: int, stamp: datetime, duration: timedelta) -> dict[str, Any]:
    return {
        "datasetId": dataset,
        "startTime": _iso_z(stamp),
        "endTime": _iso_z(stamp + duration),
        "value": _fingrid_value(dataset, stamp, duration == HOUR),
    }


def _fingrid_rows(dataset: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = [
        _fingrid_row(dataset, stamp, INTERVAL)
        for stamp in _grid(start, end)
        if stamp >= FINGRID_QUARTER_ERA
    ]
    hour = _floor_hour(start)
    while hour < end:
        if start <= hour < FINGRID_SWITCH:
            rows.append(_fingrid_row(dataset, hour, HOUR))
        hour += HOUR
    rows.sort(key=lambda row: (row["startTime"], row["endTime"]))
    return rows


def _fingrid_handler(request: httpx.Request) -> httpx.Response:
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


def _fingrid_expected(stamp: datetime) -> float:
    if stamp < FINGRID_SWITCH:
        return _fingrid_value(319, _floor_hour(stamp), hourly=True)
    return _fingrid_value(319, stamp, hourly=False)


@given(
    case=_case(
        datetime(2025, 3, 10, tzinfo=UTC),
        datetime(2025, 3, 24, tzinfo=UTC),
        _zones((BiddingZone.FI,), 1),
        end_limit=datetime(2025, 3, 25, tzinfo=UTC),
        min_step=HOUR,
    )
)
@settings(max_examples=30, deadline=None)
def test_fingrid_same_series_however_the_window_is_split(
    case: tuple[datetime, datetime, datetime, list[BiddingZone]],
) -> None:
    start, middle, end, _zones_drawn = case
    client = FingridClient(
        "test-fingrid-secret",
        httpx.Client(transport=httpx.MockTransport(_fingrid_handler)),
        sleep=lambda _: None,
        clock=lambda: 0.0,
        min_interval=0,
    )
    with pytest.MonkeyPatch().context() as patch:
        patch.setattr(fingrid, "PAGE_SIZE", 50)
        whole = client.series(319, start, end)
        assert [stamp for stamp, _ in whole] == _grid(start, end)
        for stamp, value in whole:
            assert stamp.utcoffset() == timedelta(0)
            assert stamp.minute % 15 == 0 and stamp.second == 0 and stamp.microsecond == 0
            assert start <= stamp < end
            assert value == _fingrid_expected(stamp)
        left = client.series(319, start, middle)
        right = client.series(319, middle, end)
        assert left + right == whole


# --- SvK -----------------------------------------------------------------------


def _svk_price(zone: BiddingZone, hour: datetime, direction: str) -> float:
    seed = int(hour.timestamp()) // 3600 % 991
    return float(seed) + _zone_index(zone) / 10 + (0.5 if direction == "up" else 0.0)


def _svk_record(hour: str, zone: str, direction: str, product: str, row_id: int) -> dict[str, Any]:
    start = datetime.strptime(hour, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return {
        "_id": row_id,
        "start_time_sweden": (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
        "start_time_utc": hour,
        "bidding_zone": zone,
        "reserve_product": product,
        "reserve_direction": direction,
        "price": _svk_price(BiddingZone[zone], start, direction),
        "price_unit": "EUR-MW",
        "volume": float(int(start.timestamp()) // 3600 % 500),
        "volume_unit": "MW",
        "soda_hashbyte": f"{row_id:032x}",
        "soda_identity": row_id,
    }


def _svk_handler(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    if request.url.path.endswith("/package_show"):
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"resources": [{"id": f"rid-{params['id']}", "datastore_active": True}]},
            },
        )
    filters = json.loads(params["filters"])
    product = "mFRRCapacityMarket" if "mfrr" in params["resource_id"] else "aFRRCapacityMarket"
    records = [
        _svk_record(hour, zone, direction, product, index)
        for index, (hour, zone, direction) in enumerate(
            (hour, zone, direction)
            for hour in sorted(filters["start_time_utc"])
            for zone in sorted(filters["bidding_zone"])
            for direction in ("down", "up")
        )
    ]
    offset = int(params["offset"])
    limit = int(params["limit"])
    return httpx.Response(
        200,
        json={
            "success": True,
            "result": {"total": len(records), "records": records[offset : offset + limit]},
        },
    )


@given(
    case=_case(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 3, 1, tzinfo=UTC),
        _zones((BiddingZone.SE1, BiddingZone.SE2, BiddingZone.SE3, BiddingZone.SE4), 3),
        end_limit=datetime(2026, 4, 1, tzinfo=UTC),
        min_step=HOUR,
    ),
    product=st.sampled_from((ReserveProduct.MFRR, ReserveProduct.AFRR)),
)
@settings(max_examples=30, deadline=None)
def test_svk_same_records_however_the_window_is_split(
    case: tuple[datetime, datetime, datetime, list[BiddingZone]],
    product: ReserveProduct,
) -> None:
    start, middle, end, zones = case
    client = SvKClient(
        httpx.Client(transport=httpx.MockTransport(_svk_handler)), sleep=lambda _: None
    )
    whole = client.capacity_market(product, start, end, zones)
    keys = [(r.start, r.zone, r.direction) for r in whole]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    for record in whole:
        assert record.start.utcoffset() == timedelta(0)
        assert record.start.minute % 15 == 0
        assert record.start.second == 0 and record.start.microsecond == 0
        assert start <= record.start < end
        assert record.resolution == HOUR
        direction = "up" if record.direction is Direction.UP else "down"
        assert record.price_eur_per_mw == _svk_price(
            record.zone, _floor_hour(record.start), direction
        )
    expected = _grid(start, end)
    for zone in set(zones):
        for side in (Direction.UP, Direction.DOWN):
            assert (
                sorted(r.start for r in whole if r.zone == zone and r.direction == side) == expected
            )
    left = client.capacity_market(product, start, middle, zones)
    right = client.capacity_market(product, middle, end, zones)
    tuples = [(r.start, r.zone, r.direction, r.price_eur_per_mw, r.resolution) for r in whole]
    assert [
        (r.start, r.zone, r.direction, r.price_eur_per_mw, r.resolution) for r in left + right
    ] == tuples


# --- ENTSO-E -------------------------------------------------------------------


def _entsoe_value(zone: BiddingZone, stamp: datetime) -> float:
    return float(int(stamp.timestamp()) // 900 % 997) + _zone_index(zone) / 100


def _entsoe_handler(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    start = datetime.strptime(params["periodStart"], "%Y%m%d%H%M").replace(tzinfo=UTC)
    end = datetime.strptime(params["periodEnd"], "%Y%m%d%H%M").replace(tzinfo=UTC)
    eic = params["controlArea_Domain"]
    zone = _ENTSOE_ZONE[eic]
    points = "".join(
        f"<Point><position>{index}</position>"
        f"<imbalance_Price.amount>{_entsoe_value(zone, stamp)}</imbalance_Price.amount>"
        "</Point>"
        for index, stamp in enumerate(_grid(start, end), start=1)
    )
    body = (
        f'<Balancing_MarketDocument xmlns="{NS_45}"><type>A85</type>'
        f'<area_Domain.mRID codingScheme="A01">{eic}</area_Domain.mRID>'
        "<TimeSeries><currency_Unit.name>EUR</currency_Unit.name>"
        "<curveType>A01</curveType><Period>"
        f"<timeInterval><start>{start.strftime('%Y-%m-%dT%H:%MZ')}</start>"
        f"<end>{end.strftime('%Y-%m-%dT%H:%MZ')}</end></timeInterval>"
        f"<resolution>PT15M</resolution>{points}</Period></TimeSeries>"
        "</Balancing_MarketDocument>"
    )
    return httpx.Response(200, text=body)


@given(
    case=_case(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 3, 1, tzinfo=UTC),
        _zones(_ZONES, 2),
        end_limit=datetime(2026, 4, 1, tzinfo=UTC),
        min_step=INTERVAL,
    )
)
@settings(max_examples=30, deadline=None)
def test_entsoe_same_records_however_the_window_is_split(
    case: tuple[datetime, datetime, datetime, list[BiddingZone]],
) -> None:
    start, middle, end, zones = case
    client = EntsoeClient(
        "test-entsoe-token",
        httpx.Client(transport=httpx.MockTransport(_entsoe_handler)),
        sleep=lambda _: None,
    )
    with pytest.MonkeyPatch().context() as patch:
        patch.setattr(entsoe, "_CHUNK", timedelta(days=7))
        whole = client.imbalance_prices(start, end, zones)
        _assert_price_invariants(whole, zones, start, end)
        left = client.imbalance_prices(start, middle, zones)
        right = client.imbalance_prices(middle, end, zones)
    assert _price_tuples(left + right) == _price_tuples(whole)
    for price in whole:
        assert price.resolution == INTERVAL
        assert price.imbalance_price_eur == _entsoe_value(price.zone, price.start)


# --- Reconciliation ------------------------------------------------------------


@st.composite
def _imbalance_series(draw: st.DrawFn, source: str) -> list[ImbalancePrice]:
    keys = draw(
        st.lists(
            st.tuples(
                st.sampled_from(BiddingZone),
                st.datetimes(
                    min_value=datetime(2020, 1, 1),
                    max_value=datetime(2030, 1, 1),
                    timezones=st.just(UTC),
                ),
            ),
            unique=True,
            min_size=1,
            max_size=8,
        )
    )
    return [
        ImbalancePrice(
            start=start,
            zone=zone,
            imbalance_price_eur=draw(st.one_of(st.none(), st.floats(-1000, 1000))),
            imbalance_price_dkk=None,
            spot_price_eur=None,
            dominating_direction=draw(st.one_of(st.none(), st.sampled_from(Direction))),
            satisfied_demand_mw=None,
            afrr_up_vwa_eur=None,
            afrr_down_vwa_eur=None,
            mfrr_up_price_eur=None,
            mfrr_down_price_eur=None,
            source=source,
            resolution=draw(st.sampled_from((INTERVAL, HOUR))),
            raw={},
        )
        for zone, start in keys
    ]


@given(records=_imbalance_series("a"))
@settings(max_examples=30, deadline=None)
def test_reconcile_identical_input_has_no_divergences(records: list[ImbalancePrice]) -> None:
    report = reconcile_imbalance_prices(records, records)
    assert report.divergences == ()
    assert report.identical == report.compared


@given(primary=_imbalance_series("a"), reference=_imbalance_series("b"))
@settings(max_examples=30, deadline=None)
def test_reconcile_swapping_arguments_swaps_missing_counts(
    primary: list[ImbalancePrice], reference: list[ImbalancePrice]
) -> None:
    forward = reconcile_imbalance_prices(primary, reference).counts()
    reverse = reconcile_imbalance_prices(reference, primary).counts()
    swapped = {
        DivergenceKind.MISSING_IN_PRIMARY: DivergenceKind.MISSING_IN_REFERENCE,
        DivergenceKind.MISSING_IN_REFERENCE: DivergenceKind.MISSING_IN_PRIMARY,
    }
    for kind in DivergenceKind:
        assert forward.get(kind, 0) == reverse.get(swapped.get(kind, kind), 0)


@dataclass(frozen=True, slots=True)
class _EntsoePeriodSpec:
    curve_type: str
    resolution: str
    start: datetime
    count: int
    positions: frozenset[int]
    amounts: dict[int, float]

    @property
    def step(self) -> timedelta:
        return INTERVAL if self.resolution == "PT15M" else HOUR

    @property
    def end(self) -> datetime:
        return self.start + self.count * self.step

    def is_valid_for(self, window_start: datetime, window_end: datetime) -> bool:
        if any(
            not window_start <= self.start + (pos - 1) * self.step < window_end
            for pos in self.positions
        ):
            return False
        if self.curve_type == "A01":
            return set(self.positions) == set(range(1, self.count + 1))
        return 1 in self.positions

    def series_xml(self) -> str:
        points = "".join(
            f"<Point><position>{pos}</position>"
            f"<imbalance_Price.amount>{self.amounts[pos]}</imbalance_Price.amount></Point>"
            for pos in sorted(self.positions)
        )
        return (
            "<TimeSeries><currency_Unit.name>EUR</currency_Unit.name>"
            f"<curveType>{self.curve_type}</curveType><Period>"
            f"<timeInterval><start>{self.start.strftime('%Y-%m-%dT%H:%MZ')}</start>"
            f"<end>{self.end.strftime('%Y-%m-%dT%H:%MZ')}</end></timeInterval>"
            f"<resolution>{self.resolution}</resolution>{points}</Period></TimeSeries>"
        )


def _entsoe_document_xml(eic: str, periods: tuple[_EntsoePeriodSpec, ...]) -> str:
    body = "".join(period.series_xml() for period in periods)
    return (
        f'<Balancing_MarketDocument xmlns="{NS_45}"><type>A85</type>'
        f'<area_Domain.mRID codingScheme="A01">{eic}</area_Domain.mRID>'
        f"{body}</Balancing_MarketDocument>"
    )


@st.composite
def _entsoe_period_spec(
    draw: st.DrawFn,
    anchor_end: datetime,
    max_back: int,
    max_overhang: int,
    tag_base: float,
) -> _EntsoePeriodSpec:
    curve_type = draw(st.sampled_from(("A01", "A03")))
    resolution = draw(st.sampled_from(("PT15M", "PT60M")))
    step = INTERVAL if resolution == "PT15M" else HOUR
    steps_before_end = draw(st.integers(1, max_back))
    period_start = anchor_end - steps_before_end * step
    mode = draw(
        st.sampled_from(
            ("in_window", "overhang", "arbitrary") if max_overhang else ("in_window", "arbitrary")
        )
    )
    if mode == "in_window":
        count = draw(st.integers(1, steps_before_end))
        positions = (
            draw(st.sets(st.integers(1, count)).map(lambda rest: frozenset({1, *rest})))
            if curve_type == "A03"
            else frozenset(range(1, count + 1))
        )
    elif mode == "overhang":
        count = steps_before_end + draw(st.integers(1, max_overhang))
        positions = draw(
            st.sets(st.integers(1, steps_before_end)).map(lambda rest: frozenset({1, *rest}))
        )
    else:
        count = draw(st.integers(1, steps_before_end + max_overhang))
        positions = draw(st.sets(st.integers(1, count), min_size=1).map(frozenset))
    amounts = {
        pos: tag_base + pos * 10.0 + draw(st.integers(-50, 50)) / 100.0 for pos in sorted(positions)
    }
    return _EntsoePeriodSpec(
        curve_type=curve_type,
        resolution=resolution,
        start=period_start,
        count=count,
        positions=positions,
        amounts=amounts,
    )


@st.composite
def _entsoe_two_chunk_case(
    draw: st.DrawFn,
) -> tuple[
    datetime,
    datetime,
    BiddingZone,
    tuple[datetime, datetime, tuple[_EntsoePeriodSpec, ...]],
    tuple[datetime, datetime, tuple[_EntsoePeriodSpec, ...]],
]:
    day0 = datetime(2026, 1, 15, tzinfo=UTC)
    day1 = datetime(2026, 1, 16, tzinfo=UTC)
    start = day0 + timedelta(microseconds=draw(st.integers(0, _us(HOUR) - 1)))
    end = day1 + timedelta(microseconds=draw(st.integers(_us(HOUR) + 1, _us(2 * HOUR))))
    zone = draw(st.sampled_from(_ZONES))
    early0 = (
        (draw(_entsoe_period_spec(day0 + 3 * HOUR, 2, 0, 100.0)),) if draw(st.booleans()) else ()
    )
    boundary0 = draw(_entsoe_period_spec(day1, 3, 2, 200.0))
    chunk0 = (day0, day1, (*early0, boundary0))
    chunk1 = (
        day1,
        day1 + 2 * HOUR,
        (draw(_entsoe_period_spec(day1 + 2 * HOUR, 2, 2, 1000.0)),),
    )
    return start, end, zone, chunk0, chunk1


@given(case=_entsoe_two_chunk_case())
@example(
    case=(
        datetime(2026, 1, 15, tzinfo=UTC),
        datetime(2026, 1, 16, 1, 0, 0, 1, tzinfo=UTC),
        BiddingZone.DK1,
        (
            datetime(2026, 1, 15, tzinfo=UTC),
            datetime(2026, 1, 16, tzinfo=UTC),
            (
                _EntsoePeriodSpec(
                    curve_type="A03",
                    resolution="PT15M",
                    start=datetime(2026, 1, 15, 23, 45, tzinfo=UTC),
                    count=2,
                    positions=frozenset({1}),
                    amounts={1: 210.0},
                ),
            ),
        ),
        (
            datetime(2026, 1, 16, tzinfo=UTC),
            datetime(2026, 1, 16, 2, tzinfo=UTC),
            (
                _EntsoePeriodSpec(
                    curve_type="A01",
                    resolution="PT15M",
                    start=datetime(2026, 1, 16, 1, 45, tzinfo=UTC),
                    count=1,
                    positions=frozenset({1}),
                    amounts={1: 1010.0},
                ),
            ),
        ),
    )
)
@settings(max_examples=30, deadline=None)
def test_entsoe_periods_return_only_published_in_window_records(
    case: tuple[
        datetime,
        datetime,
        BiddingZone,
        tuple[datetime, datetime, tuple[_EntsoePeriodSpec, ...]],
        tuple[datetime, datetime, tuple[_EntsoePeriodSpec, ...]],
    ],
) -> None:
    """Generated A01/A03 responses must raise SourceError whenever a Period has
    an explicit point outside its chunk request window, an A01 Period omits any
    position in 1..count, or an A03 Period omits position 1; otherwise every
    returned record lies in both its request window and a Period of that response."""
    start, end, zone, chunk0, chunk1 = case
    chunks = (chunk0, chunk1)
    by_window = {(w_start, w_end): periods for w_start, w_end, periods in chunks}

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        req_start = datetime.strptime(params["periodStart"], "%Y%m%d%H%M").replace(tzinfo=UTC)
        req_end = datetime.strptime(params["periodEnd"], "%Y%m%d%H%M").replace(tzinfo=UTC)
        periods = by_window[(req_start, req_end)]
        return httpx.Response(200, text=_entsoe_document_xml(params["controlArea_Domain"], periods))

    client = EntsoeClient(
        "test-entsoe-token",
        httpx.Client(transport=httpx.MockTransport(handler)),
        max_workers=1,
        sleep=lambda _: None,
    )
    valid = all(
        spec.is_valid_for(w_start, w_end) for w_start, w_end, periods in chunks for spec in periods
    )
    with pytest.MonkeyPatch().context() as patch:
        patch.setattr(entsoe, "_CHUNK", timedelta(days=1))
        if not valid:
            with pytest.raises(SourceError):
                client.imbalance_prices(start, end, [zone])
            return
        prices = client.imbalance_prices(start, end, [zone])

    _assert_grid(prices, start, end)
    expected: list[tuple[datetime, float, timedelta]] = []
    for _w_start, w_end, periods in chunks:
        for spec in periods:
            carried = 0.0
            for pos in range(1, spec.count + 1):
                point_start = spec.start + (pos - 1) * spec.step
                if point_start >= w_end:
                    break
                if pos in spec.positions:
                    carried = spec.amounts[pos]
                quarters = 4 if spec.resolution == "PT60M" else 1
                for quarter in range(quarters):
                    q_start = point_start + quarter * INTERVAL
                    if start <= q_start < end:
                        expected.append((q_start, carried, spec.step))

    for price in prices:
        assert price.imbalance_price_eur is not None
        origin_idx = 0 if price.imbalance_price_eur < 500.0 else 1
        req_start, req_end, origin_periods = chunks[origin_idx]
        assert req_start <= price.start < req_end
        assert any(spec.start <= price.start < spec.end for spec in origin_periods)
        assert (price.start < chunk1[0]) == (origin_idx == 0)

    assert [(p.start, p.imbalance_price_eur, p.resolution) for p in prices] == expected


@st.composite
def _esett_hourly_response_case(
    draw: st.DrawFn,
) -> tuple[datetime, datetime, list[BiddingZone], list[dict[str, Any]]]:
    base = datetime(2023, 5, 10, tzinfo=UTC)
    start = base + timedelta(microseconds=draw(st.integers(0, _us(timedelta(hours=4)))))
    first_hour = _ceil_hour(start)
    min_length = max(_us(timedelta(minutes=1)), _us(first_hour - start) + 1)
    length = draw(st.integers(min_length, _us(timedelta(hours=6))))
    end = start + timedelta(microseconds=length)
    zones = draw(_zones(_ZONES, 3))
    hours: list[datetime] = []
    hour = _floor_hour(start)
    while hour < end:
        hours.append(hour)
        hour += HOUR
    candidates = [(zone, h) for zone in zones for h in hours]
    chosen = draw(
        st.lists(
            st.sampled_from(candidates),
            unique_by=lambda pair: (pair[0], pair[1]),
        )
    )
    # A row on the first hour boundary in the window always expands to quarters
    # in [start, end), so the response is never empty.
    first = (draw(st.sampled_from(zones)), first_hour)
    if first not in chosen:
        chosen.append(first)
    finite = st.floats(-500, 500, allow_nan=False, allow_infinity=False)
    rows: list[dict[str, Any]] = []
    for zone, h in chosen:
        single = draw(st.one_of(st.none(), finite))
        mode = draw(st.sampled_from(("both", "sales", "purchase")))
        sales = single if mode in ("both", "sales") else None
        purchase = single if mode in ("both", "purchase") else None
        rows.append(
            {
                "timestampUTC": _iso_z(h),
                "mba": zone.value,
                "imblSalesPrice": sales,
                "imblPurchasePrice": purchase,
                "upRegPrice": draw(st.one_of(st.none(), finite)),
                "downRegPrice": draw(st.one_of(st.none(), finite)),
                "mainDirRegPowerPerMBA": draw(st.sampled_from((None, -1.0, 0.0, 1.0))),
            }
        )
    return start, end, zones, rows


@given(case=_esett_hourly_response_case())
@settings(max_examples=30, deadline=None)
def test_esett_hourly_quarters_map_to_published_row(
    case: tuple[datetime, datetime, list[BiddingZone], list[dict[str, Any]]],
) -> None:
    start, end, zones, rows = case
    client = ESettClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rows))),
        sleep=lambda _: None,
    )
    prices = client.imbalance_prices(start, end, zones)
    _assert_grid(prices, start, end)
    keys = [(p.start, p.zone) for p in prices]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    parsed_rows = [
        (BiddingZone[row["mba"]], datetime.fromisoformat(row["timestampUTC"]), row) for row in rows
    ]
    for price in prices:
        assert price.resolution == HOUR
        matching = [
            row
            for r_zone, r_hour, row in parsed_rows
            if r_zone is price.zone and r_hour <= price.start < r_hour + HOUR
        ]
        assert len(matching) == 1
        row = matching[0]
        expected_single = (
            row["imblSalesPrice"] if row["imblSalesPrice"] is not None else row["imblPurchasePrice"]
        )
        expected_dir = (
            None
            if row["mainDirRegPowerPerMBA"] is None
            else Direction(int(row["mainDirRegPowerPerMBA"]))
        )
        assert price.imbalance_price_eur == expected_single
        assert price.mfrr_up_price_eur == row["upRegPrice"]
        assert price.mfrr_down_price_eur == row["downRegPrice"]
        assert price.dominating_direction == expected_dir
        assert price.raw == row
    expected_keys = sorted(
        (r_hour + q * INTERVAL, r_zone)
        for r_zone, r_hour, _ in parsed_rows
        for q in range(4)
        if start <= r_hour + q * INTERVAL < end
    )
    assert keys == expected_keys


@st.composite
def _svk_response_case(
    draw: st.DrawFn,
) -> tuple[datetime, datetime, list[BiddingZone], ReserveProduct, list[dict[str, Any]]]:
    base = datetime(2026, 1, 10, tzinfo=UTC)
    start = base + timedelta(microseconds=draw(st.integers(0, _us(timedelta(hours=4)))))
    first_hour = _ceil_hour(start)
    min_length = max(_us(timedelta(minutes=1)), _us(first_hour - start) + 1)
    length = draw(st.integers(min_length, _us(timedelta(hours=6))))
    end = start + timedelta(microseconds=length)
    zones = draw(_zones((BiddingZone.SE1, BiddingZone.SE2, BiddingZone.SE3, BiddingZone.SE4), 3))
    product = draw(st.sampled_from((ReserveProduct.MFRR, ReserveProduct.AFRR)))
    product_name = "mFRRCapacityMarket" if product is ReserveProduct.MFRR else "aFRRCapacityMarket"
    hours: list[datetime] = []
    hour = _floor_hour(start)
    while hour < end:
        hours.append(hour)
        hour += HOUR
    candidates = [
        (h, zone, direction) for h in hours for zone in zones for direction in ("down", "up")
    ]
    chosen = draw(
        st.lists(
            st.sampled_from(candidates),
            unique_by=lambda item: (item[0], item[1], item[2]),
        )
    )
    # A row on the first hour boundary in the window always expands to quarters
    # in [start, end), so the response is never empty.
    first = (first_hour, draw(st.sampled_from(zones)), draw(st.sampled_from(("down", "up"))))
    if first not in chosen:
        chosen.append(first)
    finite_price = st.floats(-500, 500, allow_nan=False, allow_infinity=False)
    finite_vol = st.floats(0, 2000, allow_nan=False, allow_infinity=False)
    rows: list[dict[str, Any]] = []
    for row_id, (h, zone, direction) in enumerate(chosen, start=1):
        rows.append(
            {
                "_id": row_id,
                "start_time_sweden": (h + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                "start_time_utc": h.strftime("%Y-%m-%dT%H:00:00"),
                "bidding_zone": zone.value,
                "reserve_product": product_name,
                "reserve_direction": direction,
                "price": draw(st.one_of(st.none(), finite_price)),
                "price_unit": "EUR-MW",
                "volume": draw(st.one_of(st.none(), finite_vol)),
                "volume_unit": "MW",
                "soda_hashbyte": f"{row_id:032x}",
                "soda_identity": row_id,
            }
        )
    return start, end, zones, product, rows


@given(case=_svk_response_case())
@settings(max_examples=30, deadline=None)
def test_svk_quarters_map_to_published_row(
    case: tuple[datetime, datetime, list[BiddingZone], ReserveProduct, list[dict[str, Any]]],
) -> None:
    start, end, zones, product, rows = case

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "resources": [
                            {"id": f"rid-{request.url.params['id']}", "datastore_active": True}
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={"success": True, "result": {"total": len(rows), "records": rows}},
        )

    client = SvKClient(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
    records = client.capacity_market(product, start, end, zones)
    keys = [(r.start, r.zone, r.direction) for r in records]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    parsed_rows = [
        (
            datetime.fromisoformat(row["start_time_utc"]).replace(tzinfo=UTC),
            BiddingZone[row["bidding_zone"]],
            Direction.UP if row["reserve_direction"] == "up" else Direction.DOWN,
            row,
        )
        for row in rows
    ]
    for record in records:
        assert record.start.utcoffset() == timedelta(0)
        assert record.start.minute % 15 == 0
        assert record.start.second == 0 and record.start.microsecond == 0
        assert start <= record.start < end
        assert record.resolution == HOUR
        assert record.product is product
        matching = [
            row
            for r_hour, r_zone, r_dir, row in parsed_rows
            if r_zone is record.zone
            and r_dir is record.direction
            and r_hour <= record.start < r_hour + HOUR
        ]
        assert len(matching) == 1
        row = matching[0]
        assert record.price_eur_per_mw == row["price"]
        assert record.volume_mw == row["volume"]
        assert record.raw == row
    expected_keys = sorted(
        (r_hour + q * INTERVAL, r_zone, r_dir)
        for r_hour, r_zone, r_dir, _ in parsed_rows
        for q in range(4)
        if start <= r_hour + q * INTERVAL < end
    )
    assert keys == expected_keys


@st.composite
def _fingrid_hourly_response_case(
    draw: st.DrawFn,
) -> tuple[datetime, datetime, dict[int, list[dict[str, Any]]]]:
    base = datetime(2025, 3, 10, tzinfo=UTC)
    start = base + timedelta(microseconds=draw(st.integers(0, _us(timedelta(hours=4)))))
    first_hour = _ceil_hour(start)
    min_length = max(_us(timedelta(minutes=1)), _us(first_hour - start) + 1)
    length = draw(st.integers(min_length, _us(timedelta(hours=6))))
    end = start + timedelta(microseconds=length)
    hours: list[datetime] = []
    hour = _floor_hour(start)
    while hour < end:
        hours.append(hour)
        hour += HOUR
    finite = st.floats(-500, 500, allow_nan=False, allow_infinity=False)
    by_dataset: dict[int, list[dict[str, Any]]] = {}
    for dataset in (319, 244, 106, 369):
        chosen_hours = draw(st.lists(st.sampled_from(hours), unique=True))
        if dataset == 319 and first_hour not in chosen_hours:
            # A dataset-319 row on the first hour boundary in the window always
            # expands to a quarter in [start, end): the response is never empty.
            chosen_hours.append(first_hour)
        value_strategy = (
            st.sampled_from((None, -1.0, 0.0, 1.0))
            if dataset == 369
            else st.one_of(st.none(), finite)
        )
        by_dataset[dataset] = [
            {
                "datasetId": dataset,
                "startTime": _iso_z(h),
                "endTime": _iso_z(h + HOUR),
                "value": draw(value_strategy),
            }
            for h in chosen_hours
        ]
    return start, end, by_dataset


@given(case=_fingrid_hourly_response_case())
@settings(max_examples=30, deadline=None)
def test_fingrid_hourly_quarters_map_to_published_row(
    case: tuple[datetime, datetime, dict[int, list[dict[str, Any]]]],
) -> None:
    start, end, by_dataset = case

    def handler(request: httpx.Request) -> httpx.Response:
        dataset = int(request.url.path.split("/")[-2])
        rows = by_dataset[dataset]
        return httpx.Response(
            200,
            json={"data": rows, "pagination": {"total": len(rows), "lastPage": 1}},
        )

    client = FingridClient(
        "test-fingrid-secret",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
        clock=lambda: 0.0,
        min_interval=0,
    )
    parsed = {
        dataset: [(datetime.fromisoformat(row["startTime"]), row) for row in rows]
        for dataset, rows in by_dataset.items()
    }
    for dataset in (319, 244, 106, 369):
        series = client.series(dataset, start, end)
        for stamp, value in series:
            assert stamp.utcoffset() == timedelta(0)
            assert stamp.minute % 15 == 0 and stamp.second == 0 and stamp.microsecond == 0
            assert start <= stamp < end
            matching = [row for r_hour, row in parsed[dataset] if r_hour <= stamp < r_hour + HOUR]
            assert len(matching) == 1
            assert value == matching[0]["value"]
        expected_stamps = sorted(
            r_hour + q * INTERVAL
            for r_hour, _ in parsed[dataset]
            for q in range(4)
            if start <= r_hour + q * INTERVAL < end
        )
        assert [stamp for stamp, _ in series] == expected_stamps

    prices = client.imbalance_prices(start, end)
    _assert_grid(prices, start, end)
    for price in prices:
        assert price.zone is BiddingZone.FI
        assert price.resolution == HOUR
        matching_319 = [row for r_hour, row in parsed[319] if r_hour <= price.start < r_hour + HOUR]
        assert len(matching_319) == 1
        assert price.imbalance_price_eur == matching_319[0]["value"]
        assert price.raw["319"] == matching_319[0]
        for dataset, actual in (
            (244, price.mfrr_up_price_eur),
            (106, price.mfrr_down_price_eur),
        ):
            matching = [
                row for r_hour, row in parsed[dataset] if r_hour <= price.start < r_hour + HOUR
            ]
            assert len(matching) <= 1
            if matching:
                assert actual == matching[0]["value"]
                assert price.raw[str(dataset)] == matching[0]
            else:
                assert actual is None
                assert str(dataset) not in price.raw
        matching_369 = [row for r_hour, row in parsed[369] if r_hour <= price.start < r_hour + HOUR]
        assert len(matching_369) <= 1
        if matching_369:
            raw_dir = matching_369[0]["value"]
            assert price.dominating_direction == (
                None if raw_dir is None else Direction(int(raw_dir))
            )
            assert price.raw["369"] == matching_369[0]
        else:
            assert price.dominating_direction is None
            assert "369" not in price.raw
