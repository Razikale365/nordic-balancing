"""FCR fixtures trimmed from the orchestrator's 2026-10-15 source samples."""

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EnergiDataServiceClient,
    EntsoeClient,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
)
from nordic_balancing.sources import energidataservice, entsoe

FIXTURES = Path(__file__).parent / "fixtures" / "fcr"
START = datetime(2026, 10, 5, 22, tzinfo=UTC)
HOUR = timedelta(hours=1)
ND = (ReserveProduct.FCR_N, ReserveProduct.FCR_D)


def nd_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads((FIXTURES / "eds-fcr-nd.json").read_text())
    return rows


def dk1_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads((FIXTURES / "eds-fcr-dk1.json").read_text())
    return rows


def eds_fetch(
    rows: list[dict[str, Any]],
    product: ReserveProduct = ReserveProduct.FCR_N,
    zones: tuple[BiddingZone, ...] = (BiddingZone.DK2, BiddingZone.SE3),
    *,
    start: datetime = START,
    end: datetime = START + HOUR,
) -> list[ReserveCapacity]:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"total": len(rows), "records": rows})
    )
    with httpx.Client(transport=transport) as http, EnergiDataServiceClient(http) as client:
        return client.capacity_market(product, start, end, zones=zones)


def entsoe_fetch(
    xml: bytes | None = None,
    *,
    start: datetime = START,
    end: datetime = START + 3 * HOUR,
) -> list[ReserveCapacity]:
    if xml is None:
        xml = (FIXTURES / "entsoe-no1.xml").read_bytes()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=xml))
    with httpx.Client(transport=transport) as http, EntsoeClient("test-token", http) as client:
        return client.capacity_market(ReserveProduct.FCR_N, start, end, zones=(BiddingZone.NO1,))


@pytest.mark.parametrize("product", ND)
def test_eds_nd_maps_products_directions_total_and_auctions(product: ReserveProduct) -> None:
    rows = nd_rows()
    result = eds_fetch(list(reversed(rows)), product)
    expected_directions = (
        {Direction.NONE}
        if product is ReserveProduct.FCR_N
        else {
            Direction.UP,
            Direction.DOWN,
        }
    )
    assert len(result) == 4 * 2 * len(expected_directions)
    assert {r.direction for r in result} == expected_directions
    assert result == sorted(result, key=lambda r: (r.start, r.zone, r.direction))
    for record in result:
        name = (
            "FCR-N"
            if product is ReserveProduct.FCR_N
            else {
                Direction.UP: "FCR-D upp",
                Direction.DOWN: "FCR-D ned",
            }[record.direction]
        )
        source = next(
            r
            for r in rows
            if r["PriceArea"] == record.zone
            and r["ProductName"] == name
            and r["AuctionType"] == "Total"
        )
        assert record.price_eur_per_mw == source["PriceTotalEUR"]
        assert record.volume_mw == source["PurchasedVolumeLocal"]
        assert record.product is product
        assert record.resolution == HOUR
        assert record.source == "energidataservice"
        assert all(getattr(record, f.name) is not None for f in fields(ReserveCapacity))
        assert {k: v for k, v in record.raw.items() if k != "auctions"} == source
        auctions = [
            r
            for r in reversed(rows)
            if r["PriceArea"] == record.zone
            and r["ProductName"] == name
            and r["AuctionType"] != "Total"
        ]
        assert record.raw["auctions"] == auctions
    assert len({(r.start, r.zone, r.direction) for r in result}) == len(result)


def test_eds_dk1_maps_domestic_not_cross_border_and_keeps_raw() -> None:
    row = dk1_rows()[0]
    result = eds_fetch([row], ReserveProduct.FCR, (BiddingZone.DK1,))
    assert len(result) == 4
    for record in result:
        assert record.zone is BiddingZone.DK1
        assert record.direction is Direction.NONE
        assert record.product is ReserveProduct.FCR
        assert (record.volume_mw, record.price_eur_per_mw) == (4.0, 2.97)
        assert record.raw == row
        assert record.raw["FCRabroad_MW"] == 25.0
        assert record.raw["FCRcross_EUR"] == 11.79
        assert record.resolution == HOUR


@pytest.mark.parametrize("product", ND)
@pytest.mark.parametrize(
    "zone", [BiddingZone.DK2, BiddingZone.SE1, BiddingZone.SE2, BiddingZone.SE3, BiddingZone.SE4]
)
def test_eds_nd_covers_each_advertised_zone(product: ReserveProduct, zone: BiddingZone) -> None:
    rows = [{**r, "PriceArea": zone.value} for r in nd_rows() if r["PriceArea"] == "DK2"]
    result = eds_fetch(rows, product, (zone,))
    assert result and {r.zone for r in result} == {zone}


@pytest.mark.parametrize("product", (*ND, ReserveProduct.FCR))
@pytest.mark.parametrize("default", [True, False])
def test_eds_fcr_rejects_default_and_uncovered_zones(
    product: ReserveProduct, default: bool
) -> None:
    transport = httpx.MockTransport(lambda _: pytest.fail("validation must precede HTTP"))
    with httpx.Client(transport=transport) as http, EnergiDataServiceClient(http) as client:
        coverage = "DK1" if product is ReserveProduct.FCR else "DK2, SE1, SE2, SE3 and SE4"
        with pytest.raises(ValueError, match=coverage) as exc:
            if default:
                client.capacity_market(product, START, START + HOUR)
            else:
                client.capacity_market(product, START, START + HOUR, zones=(BiddingZone.NO1,))
        assert "pass zones=" in str(exc.value)


@pytest.mark.parametrize("field", ["PurchasedVolumeLocal", "PriceTotalEUR"])
def test_eds_nd_null_values(field: str) -> None:
    rows = [{**r, field: None} for r in nd_rows()]
    result = eds_fetch(rows)
    assert len(result) == 8
    assert all(
        getattr(r, "volume_mw" if field == "PurchasedVolumeLocal" else "price_eur_per_mw") is None
        for r in result
    )


@pytest.mark.parametrize("field", ["FCRdomestic_MW", "FCRdk_EUR"])
def test_eds_domestic_null_values(field: str) -> None:
    result = eds_fetch([{**dk1_rows()[0], field: None}], ReserveProduct.FCR, (BiddingZone.DK1,))
    assert len(result) == 4
    assert all(
        getattr(r, "volume_mw" if field == "FCRdomestic_MW" else "price_eur_per_mw") is None
        for r in result
    )


@pytest.mark.parametrize("failure", ["missing", "duplicate", "unknown"])
def test_eds_nd_rejects_bad_groups(failure: str) -> None:
    rows = nd_rows()
    if failure == "missing":
        rows = [
            r
            for r in rows
            if not (
                r["PriceArea"] == "SE3"
                and r["ProductName"] == "FCR-N"
                and r["AuctionType"] == "Total"
            )
        ]
        message = "missing Total"
    elif failure == "duplicate":
        rows.append(
            next(
                r.copy()
                for r in rows
                if r["AuctionType"] == "Total" and r["ProductName"] == "FCR-N"
            )
        )
        message = "duplicate"
    else:
        rows[0]["ProductName"] = "future product"
        message = "unknown ProductName"
    with pytest.raises(SourceError, match=message):
        eds_fetch(rows)


def test_eds_total_without_auctions_and_old_auction_name() -> None:
    row = next(
        r
        for r in nd_rows()
        if r["ProductName"] == "FCR-N" and r["PriceArea"] == "DK2" and r["AuctionType"] == "Total"
    )
    result = eds_fetch([row], zones=(BiddingZone.DK2,))
    assert len(result) == 4 and result[0].raw["auctions"] == []
    old = {**row, "AuctionType": "D-2", "PurchasedVolumeLocal": None}
    result = eds_fetch([row, old], zones=(BiddingZone.DK2,))
    assert result[0].raw["auctions"] == [old]
    result[0].raw["auctions"][0]["AuctionType"] = "changed"
    assert result[1].raw["auctions"] == [old]


def test_eds_fcr_query_columns_dataset_filter_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(energidataservice, "PAGE_SIZE", 2)
    rows = [r for r in nd_rows() if r["ProductName"] == "FCR-N" and r["PriceArea"] == "DK2"]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"total": len(rows), "records": rows[offset : offset + 2]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = EnergiDataServiceClient(http).capacity_market(
            ReserveProduct.FCR_N,
            START + INTERVAL + timedelta(microseconds=1),
            START + HOUR - INTERVAL,
            zones=(BiddingZone.DK2, BiddingZone.DK2),
        )
    assert len(result) == 1 and result[0].start == START + 2 * INTERVAL
    assert len(result[0].raw["auctions"]) == 2
    assert len(seen) == 2
    params = seen[0].url.params
    assert seen[0].url.path == "/dataset/FcrNdDK2"
    assert params["timezone"] == "UTC"
    assert params["start"] == "2026-10-05T22:00" and params["end"] == "2026-10-05T22:45"
    assert json.loads(params["filter"]) == {"PriceArea": ["DK2"], "ProductName": ["FCR-N"]}
    assert set(params["columns"].split(",")) == set(rows[0])


def test_eds_fcr_d_query_filters_both_directions() -> None:
    rows = [
        r for r in nd_rows() if r["ProductName"].startswith("FCR-D") and r["PriceArea"] == "SE3"
    ]
    filters: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        filters.append(json.loads(request.url.params["filter"]))
        return httpx.Response(200, json={"total": len(rows), "records": rows})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = EnergiDataServiceClient(http).capacity_market(
            ReserveProduct.FCR_D, START, START + HOUR, zones=(BiddingZone.SE3,)
        )
    assert filters == [{"PriceArea": ["SE3"], "ProductName": ["FCR-D upp", "FCR-D ned"]}]
    assert {r.direction for r in result} == {Direction.UP, Direction.DOWN}


def test_eds_domestic_query_has_no_price_area_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dataset/FcrDK1"
        assert "filter" not in request.url.params
        assert set(request.url.params["columns"].split(",")) == set(dk1_rows()[0])
        return httpx.Response(200, json={"total": 1, "records": dk1_rows()})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = EnergiDataServiceClient(http).capacity_market(
            ReserveProduct.FCR,
            START,
            START + HOUR,
            zones=(BiddingZone.DK1,),
        )
    assert len(result) == 4


@pytest.mark.parametrize(
    ("product", "bad"),
    [
        (product, bad)
        for product in (ReserveProduct.FCR_N, ReserveProduct.FCR)
        for bad in (
            "missing",
            "time",
            "out_of_window" if product is ReserveProduct.FCR else "zone",
            "number",
            "duplicate",
        )
    ],
)
def test_eds_fcr_rejects_malformed_rows(product: ReserveProduct, bad: str) -> None:
    domestic = product is ReserveProduct.FCR
    row = (
        dk1_rows()[0]
        if domestic
        else next(
            r
            for r in nd_rows()
            if r["ProductName"] == "FCR-N"
            and r["AuctionType"] == "Total"
            and r["PriceArea"] == "DK2"
        )
    )
    zones = (BiddingZone.DK1,) if domestic else (BiddingZone.DK2,)
    if bad == "missing":
        row.pop("HourDK")
    elif bad == "time":
        row["HourUTC"] = "2026-10-05T22:15:00"
    elif bad == "out_of_window":
        row["HourUTC"] = "2026-10-05T21:00:00"
    elif bad == "zone":
        row["PriceArea"] = "NO1"
    elif bad == "number":
        row["FCRdk_EUR" if domestic else "PriceTotalEUR"] = True
    rows = [row, row.copy()] if bad == "duplicate" else [row]
    with pytest.raises(SourceError):
        eds_fetch(rows, product, zones)


def test_entsoe_parses_a03_quantity_and_price_continuation_from_zip() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("no1.xml", (FIXTURES / "entsoe-no1.xml").read_bytes())
    result = entsoe_fetch(archive.getvalue())
    assert len(result) == 12
    assert [r.start for r in result] == [START + q * INTERVAL for q in range(12)]
    assert [r.price_eur_per_mw for r in result] == [17.2] * 8 + [18.9] * 4
    assert all(r.volume_mw == 14.4 for r in result)
    assert all(r.direction is Direction.NONE and r.zone is BiddingZone.NO1 for r in result)
    assert all(r.product is ReserveProduct.FCR_N and r.source == "entsoe" for r in result)
    assert all(r.resolution == HOUR for r in result)
    assert all(getattr(r, f.name) is not None for r in result for f in fields(ReserveCapacity))
    assert result[0].raw["quantity_Measure_Unit.name"] == "MAW"
    assert result[0].raw["currency_Unit.name"] == "EUR"
    xml = (
        (FIXTURES / "entsoe-no1.xml")
        .read_bytes()
        .replace(
            b"<quantity>14.4</quantity>",
            b"<quantity>15.4</quantity>",
            1,
        )
    )
    result = entsoe_fetch(xml)
    assert [r.volume_mw for r in result] == [15.4] * 8 + [14.4] * 4


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("<type>A81</type>", "<type>A85</type>", "document type"),
        ("<process.processType>A52", "<process.processType>A16", "process.processType"),
        ("<businessType>B95", "<businessType>A96", "businessType"),
        ("<flowDirection.direction>A03", "<flowDirection.direction>A01", "flowDirection"),
        ("<quantity_Measure_Unit.name>MAW", "<quantity_Measure_Unit.name>MWH", "quantity_Measure"),
        ("<currency_Unit.name>EUR", "<currency_Unit.name>NOK", "currency"),
        ("<resolution>PT60M", "<resolution>PT15M", "resolution"),
        ("<quantity>14.4", "<quantity>NaN", "quantity"),
        ("<procurement_Price.amount>17.2", "<procurement_Price.amount>inf", "procurement"),
        ("10YNO-1--------2", "10YNO-2--------T", "does not match"),
    ],
)
def test_entsoe_rejects_wrong_capacity_metadata(old: str, new: str, message: str) -> None:
    xml = (FIXTURES / "entsoe-no1.xml").read_text().replace(old, new)
    with pytest.raises(SourceError, match=message):
        entsoe_fetch(xml.encode())


@pytest.mark.parametrize(
    "tag",
    [
        "process.processType",
        "businessType",
        "flowDirection.direction",
        "currency_Unit.name",
        "quantity_Measure_Unit.name",
        "quantity",
        "procurement_Price.amount",
    ],
)
def test_entsoe_requires_capacity_metadata_and_point_values(tag: str) -> None:
    import re

    xml = (FIXTURES / "entsoe-no1.xml").read_text()
    xml = re.sub(r"<" + re.escape(tag) + r">.*?</" + re.escape(tag) + r">", "", xml)
    with pytest.raises(SourceError, match="missing"):
        entsoe_fetch(xml.encode())


@pytest.mark.parametrize("kind", ["series", "document", "point"])
def test_entsoe_rejects_duplicate_capacity_even_equal_values(kind: str) -> None:
    xml = (FIXTURES / "entsoe-no1.xml").read_text()
    if kind == "series":
        a, b = xml.index("<TimeSeries>"), xml.index("</TimeSeries>") + len("</TimeSeries>")
        xml = xml[:b] + xml[a:b] + xml[b:]
    elif kind == "point":
        a, b = xml.index("<Point>"), xml.index("</Point>") + len("</Point>")
        xml = xml[:b] + xml[a:b] + xml[b:]
    else:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("first.xml", xml)
            output.writestr("second.xml", xml)
        with pytest.raises(SourceError, match="duplicate"):
            entsoe_fetch(archive.getvalue())
        return
    with pytest.raises(SourceError, match="duplicate"):
        entsoe_fetch(xml.encode())


@pytest.mark.parametrize(
    "product",
    [ReserveProduct.AFRR, ReserveProduct.MFRR, ReserveProduct.FCR_D, ReserveProduct.FCR, "FCR-N"],
)
def test_entsoe_rejects_other_products(product: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError, match="FCR_N"):
        client.capacity_market(product, START, START + HOUR)


@pytest.mark.parametrize("zones", [(BiddingZone.DK2,), (BiddingZone.SE3,), (), ("NO1",)])
def test_entsoe_rejects_invalid_zones(zones: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError):
        client.capacity_market(ReserveProduct.FCR_N, START, START + HOUR, zones=zones)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (START, START),
        (START, START - HOUR),
        (START.replace(tzinfo=None), START + HOUR),
        (START, (START + HOUR).replace(tzinfo=None)),
    ],
)
def test_entsoe_capacity_checks_times(start: datetime, end: datetime) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError):
        client.capacity_market(ReserveProduct.FCR_N, start, end)


def test_entsoe_capacity_query_defaults_all_no_zones_and_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = request.url.params
        assert params["documentType"] == "A81"
        assert params["businessType"] == "B95" and params["processType"] == "A52"
        assert params["Type_MarketAgreement.Type"] == "A01"
        assert params["securityToken"] == "test-token"
        assert params["periodStart"] == "202610052200"
        assert params["periodEnd"] == "202610060100"
        xml = (
            (FIXTURES / "entsoe-no1.xml")
            .read_text()
            .replace(
                "10YNO-1--------2",
                params["controlArea_Domain"],
            )
        )
        return httpx.Response(200, content=xml.encode())

    local = timezone(timedelta(hours=2))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = EntsoeClient("test-token", http).capacity_market(
            ReserveProduct.FCR_N,
            (START + INTERVAL + timedelta(microseconds=1)).astimezone(local),
            (START + 2 * HOUR + INTERVAL + timedelta(microseconds=1)).astimezone(local),
        )
    assert len(seen) == 5
    assert {r.zone for r in result} == {
        BiddingZone.NO1,
        BiddingZone.NO2,
        BiddingZone.NO3,
        BiddingZone.NO4,
        BiddingZone.NO5,
    }
    assert len(result) == 8 * 5
    assert result == sorted(result, key=lambda r: (r.start, r.zone, r.direction))
    assert min(r.start for r in result) == START + 2 * INTERVAL
    assert max(r.start for r in result) == START + 2 * HOUR + INTERVAL


def test_entsoe_capacity_chunks_and_deduplicates_zone_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe, "_CHUNK", 3 * HOUR)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        xml = (FIXTURES / "entsoe-no1.xml").read_text()
        if request.url.params["periodStart"] == "202610060100":
            xml = xml.replace("2026-10-06T01:00Z", "2026-10-06T04:00Z")
            xml = xml.replace("2026-10-05T22:00Z", "2026-10-06T01:00Z")
        return httpx.Response(200, content=xml.encode())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = EntsoeClient("test-token", http, max_workers=1).capacity_market(
            ReserveProduct.FCR_N,
            START,
            START + 6 * HOUR,
            zones=(z for z in (BiddingZone.NO1, BiddingZone.NO1)),
        )
    assert len(seen) == 2
    assert len(result) == 24
    assert len({(r.zone, r.start) for r in result}) == 24


def test_entsoe_capacity_acknowledgements_and_redaction() -> None:
    xml = (
        b"<Acknowledgement_MarketDocument><Reason><text>No matching data found</text>"
        b"</Reason></Acknowledgement_MarketDocument>"
    )
    assert entsoe_fetch(xml) == []
    with pytest.raises(SourceError, match="REDACTED_SECRET") as exc:
        entsoe_fetch(xml.replace(b"No matching data found", b"test-token invalid request"))
    assert "test-token" not in str(exc.value)


@pytest.mark.parametrize("element", ["price_Measure_Unit.name", "price_Measurement_Unit.name"])
def test_entsoe_capacity_optional_price_unit_is_mw(element: str) -> None:
    xml = (FIXTURES / "entsoe-no1.xml").read_text()
    xml = xml.replace("<curveType>", f"<{element}>MAW</{element}><curveType>")
    assert len(entsoe_fetch(xml.encode())) == 12
    with pytest.raises(SourceError, match="unsupported"):
        entsoe_fetch(xml.replace(f"<{element}>MAW", f"<{element}>MWH").encode())


def delivery_day_xml() -> bytes:
    # Keep the sample's 22:00Z boundary and extend its trimmed Period to a day.
    # Positions 1 (22:00Z) and 5 (02:00Z) leave midnight implied by the first point.
    return (
        (FIXTURES / "entsoe-no1.xml")
        .read_bytes()
        .replace(b"2026-10-06T01:00Z", b"2026-10-06T22:00Z")
        .replace(b"<position>3</position>", b"<position>5</position>")
        .replace(b"<quantity>14.4</quantity>", b"<quantity>15.4</quantity>", 1)
    )


@pytest.mark.parametrize(
    ("offset", "length", "prices", "volumes"),
    [
        pytest.param(
            2,
            4,
            [17.2] * 8 + [18.9] * 8,
            [15.4] * 8 + [14.4] * 8,
            id="midnight_start_with_prior_explicit_point",
        ),
        pytest.param(0, 2, [17.2] * 8, [15.4] * 8, id="end_mid_period"),
        pytest.param(3, 1, [17.2] * 4, [15.4] * 4, id="implied_position_inside_window"),
    ],
)
def test_entsoe_capacity_clips_whole_delivery_day_after_a03_fill(
    offset: int,
    length: int,
    prices: list[float],
    volumes: list[float],
) -> None:
    start, end = START + offset * HOUR, START + (offset + length) * HOUR
    records = entsoe_fetch(delivery_day_xml(), start=start, end=end)
    assert [r.start for r in records] == [start + q * INTERVAL for q in range(length * 4)]
    assert [r.price_eur_per_mw for r in records] == prices
    assert [r.volume_mw for r in records] == volumes
    assert all(r.resolution == HOUR for r in records)


@pytest.mark.parametrize("offset", [-2, 24])
def test_entsoe_capacity_accepts_non_overlapping_period(offset: int) -> None:
    start = START + offset * HOUR
    assert entsoe_fetch(delivery_day_xml(), start=start, end=start + HOUR) == []


def test_entsoe_capacity_two_chunks_clip_same_delivery_day_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe, "_CHUNK", 12 * HOUR)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=delivery_day_xml())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        records = EntsoeClient("test-token", http, max_workers=1).capacity_market(
            ReserveProduct.FCR_N,
            START,
            START + 24 * HOUR,
            zones=(BiddingZone.NO1,),
        )
    assert len(seen) == 2
    assert {r.url.params["periodStart"] for r in seen} == {"202610052200", "202610061000"}
    assert [r.start for r in records] == [START + q * INTERVAL for q in range(96)]
    assert len({(r.zone, r.start) for r in records}) == 96
    assert [r.price_eur_per_mw for r in records] == [17.2] * 16 + [18.9] * 80
    assert [r.volume_mw for r in records] == [15.4] * 16 + [14.4] * 80


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (b"<position>1</position>", b"<position>0</position>", "not 1-based"),
        (b"<position>5</position>", b"<position>25</position>", "beyond Period end"),
        (b"<quantity>15.4</quantity>", b"<quantity>NaN</quantity>", "quantity is not finite"),
        (b"<procurement_Price.amount>18.9", b"<procurement_Price.amount>inf", "not finite"),
        (b"2026-10-05T22:00Z", b"2026-10-05T22:15Z", "whole number"),
    ],
)
@pytest.mark.parametrize("offset", [2, 25])
def test_entsoe_capacity_validates_explicit_points_outside_window(
    old: bytes,
    new: bytes,
    message: str,
    offset: int,
) -> None:
    xml = delivery_day_xml().replace(old, new)
    start = START + offset * HOUR
    with pytest.raises(SourceError, match=message):
        entsoe_fetch(xml, start=start, end=start + HOUR)


@pytest.mark.parametrize("position", [1, 5])
def test_entsoe_capacity_rejects_duplicate_points_outside_window(position: int) -> None:
    xml = delivery_day_xml()
    marker = f"<position>{position}</position>".encode()
    a = xml.rindex(b"<Point>", 0, xml.index(marker))
    b = xml.index(b"</Point>", a) + len(b"</Point>")
    xml = xml[:b] + xml[a:b] + xml[b:]
    with pytest.raises(SourceError, match="duplicate capacity position"):
        entsoe_fetch(xml, start=START + 2 * HOUR, end=START + 3 * HOUR)


def test_entsoe_capacity_validates_a03_start_even_for_non_overlapping_period() -> None:
    xml = delivery_day_xml().replace(b"<position>1</position>", b"<position>2</position>")
    with pytest.raises(SourceError, match="missing position 1"):
        entsoe_fetch(xml, start=START + 25 * HOUR, end=START + 26 * HOUR)


@pytest.mark.parametrize("failure", ["missing", "duplicate"])
def test_eds_nd_ignores_other_product_total_problems(failure: str) -> None:
    rows = nd_rows()
    if failure == "missing":
        rows = [r for r in rows if r["ProductName"] == "FCR-N" or r["AuctionType"] != "Total"]
    else:
        rows.append(
            next(
                r.copy()
                for r in rows
                if r["ProductName"] == "FCR-D upp" and r["AuctionType"] == "Total"
            )
        )
    result = eds_fetch(rows)
    assert len(result) == 8 and all(r.product is ReserveProduct.FCR_N for r in result)
    with pytest.raises(SourceError, match="missing Total" if failure == "missing" else "duplicate"):
        eds_fetch(rows, ReserveProduct.FCR_D)


@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.FCR])
@pytest.mark.parametrize(
    "timestamp", [None, "bad", "2026-10-05", "2026-10-05T22:01:00", "2026-10-06T00:00:00+02:00"]
)
def test_eds_fcr_timestamp_errors_name_hour_utc(product: ReserveProduct, timestamp: Any) -> None:
    rows = dk1_rows() if product is ReserveProduct.FCR else nd_rows()
    rows = [{**row, "HourUTC": timestamp} for row in rows]
    zones = (
        (BiddingZone.DK1,) if product is ReserveProduct.FCR else (BiddingZone.DK2, BiddingZone.SE3)
    )
    with pytest.raises(SourceError, match="HourUTC") as exc:
        eds_fetch(rows, product, zones)
    assert "TimeUTC" not in str(exc.value)


@pytest.mark.parametrize("fail", [True, False])
def test_entsoe_capacity_shuts_down_pool_with_pending_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    cancelled: list[bool] = []

    class RecordingPool(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            cancelled.append(cancel_futures)
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(entsoe, "ThreadPoolExecutor", RecordingPool)
    xml = b"invalid XML" if fail else delivery_day_xml()
    if fail:
        with pytest.raises(SourceError, match="document is not XML"):
            entsoe_fetch(xml)
    else:
        assert entsoe_fetch(xml)
    assert cancelled == [True]
