"""B15 offline checks using the supplied 2026-10-08 source captures."""

import io
import json
import runpy
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EntsoeClient,
    FingridClient,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
)
from nordic_balancing.sources import entsoe, fingrid

FIXTURES = Path(__file__).parent / "fixtures" / "fi_no_capacity"
START = datetime(2026, 10, 7, 22, tzinfo=UTC)
HOUR = timedelta(hours=1)
END = START + 24 * HOUR


def datasets() -> dict[int, dict[str, Any]]:
    return {
        int(path.name.split("-")[1]): json.loads(path.read_text())
        for path in FIXTURES.glob("fingrid-*.json")
    }


def fingrid_fetch(
    product: ReserveProduct,
    bodies: dict[int, dict[str, Any]] | None = None,
    start: datetime = START,
    end: datetime = END,
    seen: list[httpx.Request] | None = None,
) -> list[ReserveCapacity]:
    bodies = datasets() if bodies is None else bodies

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.headers["x-api-key"] == "test-key"
        dataset = int(request.url.path.split("/")[-2])
        return httpx.Response(200, json=bodies[dataset])

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        FingridClient("test-key", http, min_interval=0) as client,
    ):
        return client.capacity_market(product, start, end)


def archive(zone: BiddingZone = BiddingZone.NO1) -> bytes:
    return (FIXTURES / f"entsoe-a81-a47-{zone.value.lower()}-2026-10-08.zip").read_bytes()


def xml_member(zone: BiddingZone = BiddingZone.NO1) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive(zone))) as source:
        return source.read(source.namelist()[0])


def entsoe_fetch(
    content: bytes | None = None,
    zone: BiddingZone = BiddingZone.NO1,
    start: datetime = START,
    end: datetime = END,
    seen: list[httpx.Request] | None = None,
    product: ReserveProduct = ReserveProduct.MFRR,
) -> list[ReserveCapacity]:
    content = archive(zone) if content is None else content

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, content=content)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        return client.capacity_market(product, start, end, zones=[zone])


@pytest.mark.parametrize(
    ("product", "direction", "price_id", "volume_id", "price", "volume"),
    [
        (ReserveProduct.FCR_N, Direction.NONE, 317, 316, 20.1, 135.0),
        (ReserveProduct.FCR_D, Direction.UP, 318, 315, 2.5, 177.3),
        (ReserveProduct.FCR_D, Direction.DOWN, 283, 281, 6.0, 249.3),
        (ReserveProduct.MFRR, Direction.UP, 329, 327, 4.0, 406.0),
        (ReserveProduct.MFRR, Direction.DOWN, 330, 328, 1.5, 700.0),
    ],
)
def test_fingrid_exact_fixture_fields(
    product: ReserveProduct,
    direction: Direction,
    price_id: int,
    volume_id: int,
    price: float,
    volume: float,
) -> None:
    bodies = datasets()
    result = fingrid_fetch(product, bodies)
    directions = (
        {Direction.NONE} if product is ReserveProduct.FCR_N else {Direction.UP, Direction.DOWN}
    )
    expected_keys = {
        (START + q * INTERVAL, BiddingZone.FI, d) for q in range(96) for d in directions
    }
    assert len(result) == len(expected_keys)
    assert {(r.start, r.zone, r.direction) for r in result} == expected_keys
    assert result == sorted(result, key=lambda r: (r.start, r.zone, r.direction))
    for quarter in range(4):
        record = next(
            r for r in result if r.start == START + quarter * INTERVAL and r.direction is direction
        )
        expected = ReserveCapacity(
            start=START + quarter * INTERVAL,
            zone=BiddingZone.FI,
            product=product,
            direction=direction,
            price_eur_per_mw=price,
            volume_mw=volume,
            source="fingrid",
            resolution=HOUR,
            raw={"price": bodies[price_id]["data"][0], "volume": bodies[volume_id]["data"][0]},
        )
        assert record == expected
        assert record.raw == expected.raw
        assert record.end == record.start + INTERVAL


@pytest.mark.parametrize("missing", [317, 316])
def test_fingrid_pairs_union_of_hours(missing: int) -> None:
    bodies = datasets()
    bodies[missing]["data"].pop(0)
    bodies[missing]["pagination"]["total"] -= 1
    result = fingrid_fetch(ReserveProduct.FCR_N, bodies)
    assert len(result) == 96
    for record in result[:4]:
        assert record.price_eur_per_mw == (None if missing == 317 else 20.1)
        assert record.volume_mw == (None if missing == 316 else 135.0)
        assert record.raw["price" if missing == 317 else "volume"] is None


@pytest.mark.parametrize("dataset", [317, 316])
def test_fingrid_rejects_duplicate_start(dataset: int) -> None:
    bodies = datasets()
    bodies[dataset]["data"].append(dict(bodies[dataset]["data"][0]))
    bodies[dataset]["pagination"]["total"] += 1
    with pytest.raises(SourceError, match="duplicate"):
        fingrid_fetch(ReserveProduct.FCR_N, bodies)


@pytest.mark.parametrize(
    "update",
    [
        {"startTime": "2026-10-07T22:15:00Z", "endTime": "2026-10-07T23:15:00Z"},
        {"endTime": "2026-10-07T22:15:00Z"},
        {"endTime": "2026-10-08T00:00:00Z"},
        {"startTime": "2026-10-07T22:00:01Z"},
        {"value": "NaN"},
        {"value": "inf"},
        {"value": "-inf"},
        {"value": True},
        {"datasetId": 319},
    ],
)
def test_fingrid_rejects_invalid_capacity_row(update: dict[str, Any]) -> None:
    bodies = datasets()
    bodies[317]["data"][0].update(update)
    with pytest.raises(SourceError):
        fingrid_fetch(ReserveProduct.FCR_N, bodies)


def test_fingrid_null_values_and_empty_datasets() -> None:
    bodies = datasets()
    bodies[317]["data"][0]["value"] = None
    assert fingrid_fetch(ReserveProduct.FCR_N, bodies)[0].price_eur_per_mw is None
    for dataset in (317, 316):
        bodies[dataset]["data"] = []
        bodies[dataset]["pagination"]["total"] = 0
    assert fingrid_fetch(ReserveProduct.FCR_N, bodies) == []


def test_fingrid_query_floor_clip_and_timezone() -> None:
    seen: list[httpx.Request] = []
    start = START + INTERVAL + timedelta(microseconds=1)
    end = END - INTERVAL + timedelta(microseconds=1)
    result = fingrid_fetch(
        ReserveProduct.MFRR,
        start=start.astimezone(timezone(timedelta(hours=2))),
        end=end,
        seen=seen,
    )
    assert len(seen) == 4
    assert all(r.url.params["startTime"] == START.isoformat().replace("+00:00", "Z") for r in seen)
    assert all(r.url.params["endTime"] == END.isoformat().replace("+00:00", "Z") for r in seen)
    assert len(result) == 94 * 2
    assert all(start <= r.start < end for r in result)


@pytest.mark.parametrize("total", [23, 25, True, -1])
def test_fingrid_capacity_checks_pagination_totals(total: Any) -> None:
    bodies = datasets()
    bodies[317]["pagination"]["total"] = total
    with pytest.raises(SourceError, match="total"):
        fingrid_fetch(ReserveProduct.FCR_N, bodies)


def test_fingrid_capacity_fetches_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fingrid, "PAGE_SIZE", 12)
    bodies = datasets()
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dataset = int(request.url.path.split("/")[-2])
        page = int(request.url.params["page"])
        calls.append((dataset, page))
        return httpx.Response(
            200,
            json={
                "data": bodies[dataset]["data"][(page - 1) * 12 : page * 12],
                "pagination": {"total": 24, "lastPage": 2},
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        FingridClient("test-key", http, min_interval=0) as client,
    ):
        result = client.capacity_market(ReserveProduct.FCR_N, START, END)
    assert len(result) == 96
    assert calls == [(317, 1), (317, 2), (316, 1), (316, 2)]


def test_fingrid_capacity_keeps_pacing() -> None:
    bodies = datasets()
    sleeps: list[float] = []
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=bodies[int(r.url.path.split("/")[-2])])
    )
    with (
        httpx.Client(transport=transport) as http,
        FingridClient("test-key", http, clock=lambda: 0, sleep=sleeps.append) as client,
    ):
        assert len(client.capacity_market(ReserveProduct.MFRR, START, END)) == 192
    assert sleeps == [2.0, 2.0, 2.0]


@pytest.mark.parametrize("product", [ReserveProduct.AFRR, ReserveProduct.FCR, "FCR-N", None])
def test_fingrid_invalid_product(product: Any) -> None:
    with (
        FingridClient("test-key") as client,
        pytest.raises(ValueError, match=r"FCR_N.*FCR_D.*MFRR"),
    ):
        client.capacity_market(product, START, END)


@pytest.mark.parametrize(
    "start,end", [(START, START), (END, START), (START.replace(tzinfo=None), END)]
)
def test_fingrid_invalid_window(start: datetime, end: datetime) -> None:
    with FingridClient("test-key") as client, pytest.raises(ValueError):
        client.capacity_market(ReserveProduct.MFRR, start, end)


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_exact_fixture_fields_and_query(zone: BiddingZone) -> None:
    seen: list[httpx.Request] = []
    result = entsoe_fetch(zone=zone, seen=seen)
    assert len(result) == 24 * 2 * 4
    assert {(r.start, r.zone, r.direction) for r in result} == {
        (START + q * INTERVAL, zone, d) for q in range(96) for d in (Direction.UP, Direction.DOWN)
    }
    assert result == sorted(result, key=lambda r: (r.start, r.zone, r.direction))
    first_values = (
        {Direction.UP: (4.0, 406.0), Direction.DOWN: (1.5, 700.0)}
        if zone is BiddingZone.FI
        else {Direction.UP: (1.5, 95.0), Direction.DOWN: (1.5, 76.0)}
    )
    for direction, (price, volume) in first_values.items():
        flow = "A01" if direction is Direction.UP else "A02"
        for quarter in range(4):
            record = next(
                r
                for r in result
                if r.start == START + quarter * INTERVAL and r.direction is direction
            )
            expected = ReserveCapacity(
                start=START + quarter * INTERVAL,
                zone=zone,
                product=ReserveProduct.MFRR,
                direction=direction,
                price_eur_per_mw=price,
                volume_mw=volume,
                source="entsoe",
                resolution=HOUR,
                raw={
                    "zone": zone.value,
                    "start": START.isoformat(),
                    "resolution": "PT60M",
                    "curveType": "A03",
                    "namespace": "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:4",
                    "quantity": volume,
                    "procurement_Price.amount": price,
                    "flow_direction": flow,
                    "businessType": "B95",
                    "currency_Unit.name": "EUR",
                    "quantity_Measure_Unit.name": "MAW",
                },
            )
            assert record == expected
            assert record.raw == expected.raw
            assert record.end == record.start + INTERVAL
    assert len(seen) == 1
    params = seen[0].url.params
    assert dict(params) == {
        "securityToken": "test-token",
        "documentType": "A81",
        "businessType": "B95",
        "processType": "A47",
        "Type_MarketAgreement.Type": "A01",
        "controlArea_Domain": entsoe._EIC_CODES[zone],
        "periodStart": "202610072200",
        "periodEnd": "202610082200",
    }


def test_fi_mfrr_offline_cross_check_all_hours_and_directions() -> None:
    fg, en = fingrid_fetch(ReserveProduct.MFRR), entsoe_fetch(zone=BiddingZone.FI)
    assert len(fg) == len(en) == 192
    assert {(r.start, r.direction): (r.price_eur_per_mw, r.volume_mw) for r in fg} == {
        (r.start, r.direction): (r.price_eur_per_mw, r.volume_mw) for r in en
    }


def test_entsoe_a03_fills_both_quantity_and_price() -> None:
    result = entsoe_fetch()
    # NO1 UP position 12 is omitted; DOWN FI positions 2 and 3 are omitted.
    for hour, price, volume in [
        (10, 1.5, 93.0),
        (11, 1.5, 93.0),
        (12, 2.9, 131.0),
        (13, 2.9, 131.0),
    ]:
        records = [
            r
            for r in result
            if r.direction is Direction.UP
            and START + hour * HOUR <= r.start < START + (hour + 1) * HOUR
        ]
        assert len(records) == 4
        assert {(r.price_eur_per_mw, r.volume_mw) for r in records} == {(price, volume)}
    fi = entsoe_fetch(zone=BiddingZone.FI)
    assert all(
        r.price_eur_per_mw == 1.5 and r.volume_mw == 700.0
        for r in fi
        if r.direction is Direction.DOWN and r.start < START + 3 * HOUR
    )


def zip_members(*members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for index, member in enumerate(members):
            target.writestr(f"{index}.xml", member)
    return buffer.getvalue()


@pytest.mark.parametrize("field", ["quantity", "procurement_Price.amount"])
def test_entsoe_conflicting_document_still_raises(field: str) -> None:
    first = xml_member()
    root = ET.fromstring(first)
    node = root.find(f"{{*}}TimeSeries/{{*}}Period/{{*}}Point/{{*}}{field}")
    assert node is not None
    node.text = "999.25"
    with pytest.raises(SourceError, match="duplicate capacity"):
        entsoe_fetch(zip_members(first, ET.tostring(root)), product=ReserveProduct.MFRR)


def test_entsoe_mfrr_identical_documents_collapse() -> None:
    xml = xml_member()
    result = entsoe_fetch(zip_members(xml, xml), product=ReserveProduct.MFRR)
    assert len(result) == 192
    assert all(r.product is ReserveProduct.MFRR for r in result)
    assert result == entsoe_fetch(product=ReserveProduct.MFRR)


@pytest.mark.parametrize("where", ["series", "document"])
def test_entsoe_fcr_identical_repeats_collapse(where: str) -> None:
    xml = (FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
    if where == "series":
        root = ET.fromstring(xml)
        series = root.find("{*}TimeSeries")
        assert series is not None
        root.append(ET.fromstring(ET.tostring(series)))
        content = ET.tostring(root)
    else:
        content = zip_members(xml, xml)
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    result = entsoe_fetch(content, start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N)
    expected = entsoe_fetch(xml, start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N)
    assert len(result) == 12
    assert result == expected
    assert [r.raw for r in result] == [r.raw for r in expected]


def test_entsoe_whole_day_clips_across_two_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entsoe, "_CHUNK", 12 * HOUR)
    seen: list[httpx.Request] = []
    chunked = entsoe_fetch(seen=seen)
    monkeypatch.setattr(entsoe, "_CHUNK", timedelta(days=366))
    assert chunked == entsoe_fetch()
    assert len(seen) == 2
    assert [r.url.params["periodStart"] for r in seen] == ["202610072200", "202610081000"]
    clipped = entsoe_fetch(start=START + INTERVAL + timedelta(microseconds=1), end=END - INTERVAL)
    assert len(clipped) == 93 * 2
    assert all(START + INTERVAL < r.start < END - INTERVAL for r in clipped)


@pytest.mark.parametrize(
    "old,new",
    [
        (b"<type>A81", b"<type>A85"),
        (b"<process.processType>A47", b"<process.processType>A52"),
        (b"<businessType>B95", b"<businessType>A96"),
        (b"<flowDirection.direction>A01", b"<flowDirection.direction>A03"),
        (b"<quantity_Measure_Unit.name>MAW", b"<quantity_Measure_Unit.name>MWH"),
        (b"<currency_Unit.name>EUR", b"<currency_Unit.name>NOK"),
        (b"<resolution>PT60M", b"<resolution>PT15M"),
        (b"<quantity>95", b"<quantity>NaN"),
        (b"<procurement_Price.amount>1.5", b"<procurement_Price.amount>inf"),
    ],
)
def test_entsoe_rejects_invalid_mfrr_metadata(old: bytes, new: bytes) -> None:
    with pytest.raises(SourceError):
        entsoe_fetch(xml_member().replace(old, new))


@pytest.mark.parametrize(
    "product", [ReserveProduct.AFRR, ReserveProduct.FCR_D, ReserveProduct.FCR, "mFRR", None]
)
def test_entsoe_invalid_product(product: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError, match=r"FCR_N.*MFRR"):
        client.capacity_market(product, START, END)


@pytest.mark.parametrize(
    "product,zone",
    [
        (ReserveProduct.FCR_N, BiddingZone.FI),
        (ReserveProduct.MFRR, BiddingZone.SE3),
        (ReserveProduct.MFRR, BiddingZone.DK1),
        (ReserveProduct.MFRR, "FI"),
    ],
)
def test_entsoe_invalid_product_zone(product: ReserveProduct, zone: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError, match=r"NO1.*NO5"):
        client.capacity_market(product, START, END, zones=[zone])


@pytest.mark.parametrize(
    "zone", [BiddingZone.NO2, BiddingZone.NO3, BiddingZone.NO4, BiddingZone.NO5]
)
def test_entsoe_all_norwegian_mfrr_zones(zone: BiddingZone) -> None:
    xml = xml_member().replace(b"10YNO-1--------2", entsoe._EIC_CODES[zone].encode())
    result = entsoe_fetch(xml, zone=zone)
    assert len(result) == 192
    assert {r.zone for r in result} == {zone}


def test_entsoe_mfrr_error_redaction() -> None:
    xml = xml_member().replace(
        b"<flowDirection.direction>A01", b"<flowDirection.direction>test-token"
    )
    with pytest.raises(SourceError) as exc:
        entsoe_fetch(xml)
    assert "test-token" not in str(exc.value)
    xml = (
        b"<Acknowledgement_MarketDocument><Reason><text>test-token denied</text>"
        b"</Reason></Acknowledgement_MarketDocument>"
    )
    with pytest.raises(SourceError, match="REDACTED_SECRET") as exc:
        entsoe_fetch(xml)
    assert "test-token" not in str(exc.value)


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_mfrr_identical_series_in_one_document_collapse(zone: BiddingZone) -> None:
    root = ET.fromstring(xml_member(zone))
    series = root.find("{*}TimeSeries")
    assert series is not None
    root.append(ET.fromstring(ET.tostring(series)))
    result = entsoe_fetch(ET.tostring(root), zone=zone)
    assert len(result) == 192
    assert result == entsoe_fetch(xml_member(zone), zone=zone)


def test_entsoe_rejects_missing_mfrr_direction() -> None:
    root = ET.fromstring(xml_member())
    series = root.find("{*}TimeSeries")
    assert series is not None
    flow = series.find("{*}flowDirection.direction")
    assert flow is not None
    series.remove(flow)
    with pytest.raises(SourceError, match="missing flowDirection"):
        entsoe_fetch(ET.tostring(root))


def test_entsoe_mfrr_empty_zones() -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError, match="empty"):
        client.capacity_market(ReserveProduct.MFRR, START, END, zones=[])


def test_evidence_comparison_detects_gaps_differences_and_empty_windows() -> None:
    compare = runpy.run_path(
        str(FIXTURES.parents[2] / "examples" / "fi_no_capacity" / "cross_check.py")
    )["compare"]
    fg = fingrid_fetch(ReserveProduct.MFRR)
    en = entsoe_fetch(zone=BiddingZone.FI)
    result = compare(en, fg, BiddingZone.FI, START, END)
    assert result["status"] == "PASS"
    assert result["matched_hour_directions"] == 48
    result = compare(en[8:], fg[8:], BiddingZone.FI, START, END)
    assert result["status"] == "FAIL"
    assert len(result["missing_hour_directions"]) == 2
    assert compare([], [], BiddingZone.FI, START, END)["status"] == "FAIL"
    from dataclasses import replace

    changed = [
        replace(r, price_eur_per_mw=999.0)
        if r.start < START + HOUR and r.direction is Direction.UP
        else r
        for r in en
    ]
    result = compare(changed, fg, BiddingZone.FI, START, END)
    assert result["status"] == "FAIL"
    assert len(result["differing_hour_directions"]) == 1


def test_evidence_script_requires_live_opt_in() -> None:
    main = runpy.run_path(
        str(FIXTURES.parents[2] / "examples" / "fi_no_capacity" / "cross_check.py")
    )["main"]
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_evidence_binary_search_checks_first_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    first_day = runpy.run_path(
        str(FIXTURES.parents[2] / "examples" / "fi_no_capacity" / "cross_check.py")
    )["first_day"]
    records = entsoe_fetch(zone=BiddingZone.FI)
    calls: list[datetime] = []
    boundary = datetime(2022, 12, 1, tzinfo=UTC)

    def available(
        product: ReserveProduct, start: datetime, end: datetime, *, zones: list[BiddingZone]
    ) -> list[ReserveCapacity]:
        assert product is ReserveProduct.MFRR and zones == [BiddingZone.FI]
        assert end - start == timedelta(days=1)
        calls.append(start)
        return records if start >= boundary else []

    with EntsoeClient("test-token") as client:
        monkeypatch.setattr(client, "capacity_market", available)
        assert (
            first_day(
                client, BiddingZone.FI, datetime(2010, 1, 1, tzinfo=UTC), START.replace(hour=0)
            )
            == boundary
        )
    assert boundary in calls and boundary - timedelta(days=1) in calls
    assert len(calls) < 20


def test_evidence_se3_uses_validated_a47_parser() -> None:
    fetch = runpy.run_path(
        str(FIXTURES.parents[2] / "examples" / "fi_no_capacity" / "cross_check.py")
    )["_entsoe_se3"]
    xml = xml_member(BiddingZone.FI).replace(b"10YFI-1--------U", b"10Y1001A1001A46L")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=zip_members(xml, xml))

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http) as client,
    ):
        result = fetch(client, START, END)
    assert len(result) == 192
    assert all(r.zone is BiddingZone.SE3 and r.product is ReserveProduct.MFRR for r in result)
    assert requests[0].url.params["processType"] == "A47"
    assert requests[0].url.params["controlArea_Domain"] == "10Y1001A1001A46L"


EARLY_DUPLICATE_FIXTURES = [
    (BiddingZone.NO1, "2024-02-15", datetime(2024, 2, 14, 23, tzinfo=UTC)),
    (BiddingZone.FI, "2024-11-13", datetime(2024, 11, 12, 23, tzinfo=UTC)),
]


def early_archive(zone: BiddingZone, delivery_day: str) -> bytes:
    return (
        FIXTURES / f"entsoe-a81-a47-{zone.value.lower()}-{delivery_day}-duplicated-series.zip"
    ).read_bytes()


def early_document(zone: BiddingZone, delivery_day: str) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(early_archive(zone, delivery_day))) as archive:
        assert len(archive.namelist()) == 1
        return ET.fromstring(archive.read(archive.namelist()[0]))


@pytest.mark.parametrize("zone,delivery_day,start", EARLY_DUPLICATE_FIXTURES)
def test_entsoe_historical_identical_series_exact_values(
    zone: BiddingZone,
    delivery_day: str,
    start: datetime,
) -> None:
    result = entsoe_fetch(
        early_archive(zone, delivery_day), zone=zone, start=start, end=start + 24 * HOUR
    )
    expected_keys = {
        (start + q * INTERVAL, zone, direction)
        for q in range(96)
        for direction in (Direction.UP, Direction.DOWN)
    }
    assert len(result) == 192
    assert {(r.start, r.zone, r.direction) for r in result} == expected_keys
    assert result == sorted(result, key=lambda r: (r.start, r.zone, r.direction))
    values = (
        [
            (0, Direction.UP, 2.0, 10.0),
            (0, Direction.DOWN, 2.0, 25.0),
            (5, Direction.UP, 3.0, 10.0),
            (3, Direction.DOWN, 2.0, 31.0),
        ]
        if zone is BiddingZone.NO1
        else [
            (0, Direction.UP, 2.0, 196.0),
            (0, Direction.DOWN, 11.0, 480.0),
            (2, Direction.UP, 1.5, 191.0),
            (1, Direction.DOWN, 7.5, 480.0),
        ]
    )
    for hour, direction, price, volume in values:
        for quarter in range(4):
            stamp = start + hour * HOUR + quarter * INTERVAL
            record = next(r for r in result if r.start == stamp and r.direction is direction)
            assert record.price_eur_per_mw == price
            assert record.volume_mw == volume
            assert record.product is ReserveProduct.MFRR
            assert record.source == "entsoe" and record.resolution == HOUR
            assert record.raw["quantity"] == volume
            assert record.raw["procurement_Price.amount"] == price
            assert record.raw["flow_direction"] == ("A01" if direction is Direction.UP else "A02")


@pytest.mark.parametrize("zone,delivery_day,start", EARLY_DUPLICATE_FIXTURES)
@pytest.mark.parametrize("field", ["quantity", "procurement_Price.amount"])
def test_entsoe_historical_conflicting_second_series_raises(
    zone: BiddingZone,
    delivery_day: str,
    start: datetime,
    field: str,
) -> None:
    root = early_document(zone, delivery_day)
    series = root.findall("{*}TimeSeries")
    assert len(series) == 4
    assert series[0].findtext("{*}flowDirection.direction") == series[1].findtext(
        "{*}flowDirection.direction"
    )
    node = series[1].find(f"{{*}}Period/{{*}}Point/{{*}}{field}")
    assert node is not None and node.text is not None
    node.text = str(float(node.text) + 0.25)
    with pytest.raises(SourceError, match="duplicate capacity with differing values for mFRR"):
        entsoe_fetch(ET.tostring(root), zone=zone, start=start, end=start + 24 * HOUR)


@pytest.mark.parametrize("zone,delivery_day,start", EARLY_DUPLICATE_FIXTURES)
def test_entsoe_historical_repeated_position_remains_invalid(
    zone: BiddingZone,
    delivery_day: str,
    start: datetime,
) -> None:
    root = early_document(zone, delivery_day)
    period = root.find("{*}TimeSeries/{*}Period")
    assert period is not None
    point = period.find("{*}Point")
    assert point is not None
    period.append(ET.fromstring(ET.tostring(point)))
    with pytest.raises(SourceError) as exc:
        entsoe_fetch(ET.tostring(root), zone=zone, start=start, end=start + 24 * HOUR)
    assert str(exc.value) == "entsoe: duplicate capacity position 1 in Period"


@pytest.mark.parametrize("zone,delivery_day,start", EARLY_DUPLICATE_FIXTURES)
def test_entsoe_compares_duplicate_series_after_a03_fill(
    zone: BiddingZone,
    delivery_day: str,
    start: datetime,
) -> None:
    root = early_document(zone, delivery_day)
    # Add a redundant explicit position 2 in the repeated series. Its A03-filled
    # values match, although its XML and explicit Point list no longer match.
    period = root.findall("{*}TimeSeries")[1].find("{*}Period")
    assert period is not None
    first = period.find("{*}Point")
    assert first is not None
    second = ET.fromstring(ET.tostring(first))
    position = second.find("{*}position")
    assert position is not None
    position.text = "2"
    period.append(second)
    result = entsoe_fetch(ET.tostring(root), zone=zone, start=start, end=start + 24 * HOUR)
    assert len(result) == 192
    assert result == entsoe_fetch(
        early_archive(zone, delivery_day), zone=zone, start=start, end=start + 24 * HOUR
    )


@pytest.mark.parametrize("zone", [BiddingZone.FI, BiddingZone.SE3, "NO1"])
def test_entsoe_fcr_zone_error_text_unchanged(zone: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError) as exc:
        client.capacity_market(ReserveProduct.FCR_N, START, END, zones=[zone])
    assert str(exc.value) == "ENTSO-E FCR-N capacity covers only NO1, NO2, NO3, NO4 and NO5"


@pytest.mark.parametrize("where", ["series", "point"])
def test_entsoe_fcr_flow_error_text_unchanged(where: str) -> None:
    root = ET.fromstring((FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes())
    if where == "series":
        node = root.find("{*}TimeSeries/{*}flowDirection.direction")
        assert node is not None
        node.text = "A01"
    else:
        point = root.find("{*}TimeSeries/{*}Period/{*}Point")
        assert point is not None
        ET.SubElement(point, root.tag.partition("}")[0] + "}flowDirection.direction").text = "A01"
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    with pytest.raises(SourceError) as exc:
        entsoe_fetch(
            ET.tostring(root), start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N
        )
    assert str(exc.value) == "entsoe: capacity flowDirection.direction must be A03"


@pytest.mark.parametrize("where", ["series", "document"])
def test_entsoe_fcr_conflicting_duplicate_error_text_unchanged(where: str) -> None:
    xml = (FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
    if where == "series":
        root = ET.fromstring(xml)
        series = root.find("{*}TimeSeries")
        assert series is not None
        changed = ET.fromstring(ET.tostring(series))
        quantity = changed.find("{*}Period/{*}Point/{*}quantity")
        assert quantity is not None
        quantity.text = "999.25"
        root.append(changed)
        content = ET.tostring(root)
    else:
        content = zip_members(xml, xml.replace(b"<quantity>14.4", b"<quantity>999.25", 1))
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    with pytest.raises(SourceError) as exc:
        entsoe_fetch(content, start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N)
    assert str(exc.value) == f"entsoe: duplicate capacity for NO1 at {start}"


def test_entsoe_mfrr_sorts_zones_before_directions() -> None:
    responses = {
        "10YNO-1--------2": archive(BiddingZone.NO1),
        "10YFI-1--------U": archive(BiddingZone.FI),
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.params["controlArea_Domain"]
        requested.append(domain)
        assert request.url.params["processType"] == "A47"
        return httpx.Response(200, content=responses[domain])

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        result = client.capacity_market(
            ReserveProduct.MFRR, START, END, zones=[BiddingZone.NO1, BiddingZone.FI]
        )
    assert len(requested) == 2
    assert set(requested) == set(responses)
    assert len(result) == 384
    # BiddingZone is a StrEnum: FI precedes NO1. Zone must sort before direction.
    zone_directions = (
        (BiddingZone.FI, Direction.DOWN),
        (BiddingZone.FI, Direction.UP),
        (BiddingZone.NO1, Direction.DOWN),
        (BiddingZone.NO1, Direction.UP),
    )
    expected_first_hour = [
        (START + quarter * INTERVAL, zone, direction)
        for quarter in range(4)
        for zone, direction in zone_directions
    ]
    keys = [(r.start, r.zone, r.direction) for r in result]
    assert keys[:8] == expected_first_hour[:8]
    # Two zones x two directions x four quarters give 16 records in the first hour.
    assert keys[:16] == expected_first_hour
    assert keys == sorted(keys)


PLACEHOLDER_START = datetime(2025, 6, 4, 22, tzinfo=UTC)
PLACEHOLDER_END = PLACEHOLDER_START + 24 * HOUR


def placeholder_archive(zone: BiddingZone) -> bytes:
    return (
        FIXTURES / f"entsoe-a81-a47-{zone.value.lower()}-2025-06-05-placeholder-series.zip"
    ).read_bytes()


def real_placeholder_document(zone: BiddingZone) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(placeholder_archive(zone))) as source:
        # Each capture has a clean member with just the real UP/DOWN series.
        for name in source.namelist():
            root = ET.fromstring(source.read(name))
            if len(root.findall("{*}TimeSeries")) == 2:
                return root
    raise AssertionError("fixture has no clean two-series member")


def placeholder_case(zone: BiddingZone) -> tuple[ET.Element, ET.Element, ET.Element]:
    root = real_placeholder_document(zone)
    for series in list(root.findall("{*}TimeSeries")):
        if series.findtext("{*}flowDirection.direction") != "A01":
            root.remove(series)
    real = root.find("{*}TimeSeries")
    assert real is not None
    placeholder = ET.fromstring(ET.tostring(real))
    period = placeholder.find("{*}Period")
    assert period is not None
    points = period.findall("{*}Point")
    first = points[0]
    for point in points[1:]:
        period.remove(point)
    for tag in ("quantity", "procurement_Price.amount"):
        value = first.find(f"{{*}}{tag}")
        assert value is not None
        value.text = "0"
    root.append(placeholder)
    return root, real, placeholder


def fetch_placeholder_case(content: bytes, zone: BiddingZone) -> list[ReserveCapacity]:
    return entsoe_fetch(content, zone=zone, start=PLACEHOLDER_START, end=PLACEHOLDER_END)


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
@pytest.mark.parametrize("reverse_members", [False, True])
def test_entsoe_placeholder_fixtures_match_real_series(
    zone: BiddingZone,
    reverse_members: bool,
) -> None:
    content = placeholder_archive(zone)
    if reverse_members:
        with zipfile.ZipFile(io.BytesIO(content)) as source:
            content = zip_members(*(source.read(name) for name in reversed(source.namelist())))
    result = fetch_placeholder_case(content, zone)
    assert len(result) == 192
    expected: dict[tuple[datetime, Direction], tuple[float, float]] = {}
    root = real_placeholder_document(zone)
    for series in root.findall("{*}TimeSeries"):
        direction = (
            Direction.UP
            if series.findtext("{*}flowDirection.direction") == "A01"
            else Direction.DOWN
        )
        explicit = {}
        for point in series.findall("{*}Period/{*}Point"):
            position, price_text, quantity_text = (
                point.findtext(f"{{*}}{tag}")
                for tag in ("position", "procurement_Price.amount", "quantity")
            )
            assert position is not None and price_text is not None and quantity_text is not None
            explicit[int(position)] = float(price_text), float(quantity_text)
        previous: tuple[float, float] | None = None
        for hour in range(24):
            previous = explicit.get(hour + 1, previous)
            assert previous is not None
            for quarter in range(4):
                expected[(PLACEHOLDER_START + hour * HOUR + quarter * INTERVAL, direction)] = (
                    previous
                )
    assert {(r.start, r.direction): (r.price_eur_per_mw, r.volume_mw) for r in result} == expected
    assert result == fetch_placeholder_case(ET.tostring(root), zone)
    assert all(r.zone is zone and r.product is ReserveProduct.MFRR for r in result)
    assert all(r.source == "entsoe" and r.resolution == HOUR for r in result)
    samples = (
        [
            (0, Direction.UP, 6.1, 35.0),
            (1, Direction.UP, 6.0, 35.0),
            (0, Direction.DOWN, 10.0, 358.0),
            (3, Direction.DOWN, 18.86, 358.0),
        ]
        if zone is BiddingZone.NO1
        else [
            (0, Direction.UP, 20.8, 380.0),
            (2, Direction.UP, 26.5, 370.0),
            (0, Direction.DOWN, 3.5, 402.0),
            (2, Direction.DOWN, 3.9, 482.0),
        ]
    )
    for hour, direction, price, volume in samples:
        records = [
            r
            for r in result
            if r.direction is direction
            and PLACEHOLDER_START + hour * HOUR <= r.start < PLACEHOLDER_START + (hour + 1) * HOUR
        ]
        assert len(records) == 4
        assert {(r.price_eur_per_mw, r.volume_mw) for r in records} == {(price, volume)}


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
@pytest.mark.parametrize("copies", [1, 2])
def test_entsoe_lone_placeholders_are_kept(zone: BiddingZone, copies: int) -> None:
    root, real, _ = placeholder_case(zone)
    root.remove(real)
    xml = ET.tostring(root)
    result = fetch_placeholder_case(zip_members(*(xml for _ in range(copies))), zone)
    assert len(result) == 96
    assert {r.start for r in result} == {PLACEHOLDER_START + q * INTERVAL for q in range(96)}
    assert all(r.direction is Direction.UP and r.product is ReserveProduct.MFRR for r in result)
    assert all(r.price_eur_per_mw == 0.0 and r.volume_mw == 0.0 for r in result)


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
@pytest.mark.parametrize("field", ["quantity", "procurement_Price.amount"])
def test_entsoe_one_point_nonzero_series_still_conflicts(zone: BiddingZone, field: str) -> None:
    root, _, placeholder = placeholder_case(zone)
    node = placeholder.find(f"{{*}}Period/{{*}}Point/{{*}}{field}")
    assert node is not None
    node.text = "0.25"
    with pytest.raises(SourceError, match="duplicate capacity with differing values for mFRR"):
        fetch_placeholder_case(ET.tostring(root), zone)


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_multiple_zero_points_still_conflict(zone: BiddingZone) -> None:
    root, _, placeholder = placeholder_case(zone)
    period = placeholder.find("{*}Period")
    assert period is not None
    first = period.find("{*}Point")
    assert first is not None
    second = ET.fromstring(ET.tostring(first))
    position = second.find("{*}position")
    assert position is not None
    position.text = "2"
    period.append(second)
    with pytest.raises(SourceError, match="duplicate capacity with differing values for mFRR"):
        fetch_placeholder_case(ET.tostring(root), zone)


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_matching_zero_real_series_is_unchanged(zone: BiddingZone) -> None:
    root, real, placeholder = placeholder_case(zone)
    for point in real.findall("{*}Period/{*}Point"):
        for field in ("quantity", "procurement_Price.amount"):
            node = point.find(f"{{*}}{field}")
            assert node is not None
            node.text = "0"
    with_placeholder = fetch_placeholder_case(ET.tostring(root), zone)
    root.remove(placeholder)
    assert with_placeholder == fetch_placeholder_case(ET.tostring(root), zone)
    assert len(with_placeholder) == 96
    assert all(r.price_eur_per_mw == r.volume_mw == 0.0 for r in with_placeholder)


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_placeholder_needs_real_series_in_same_direction(zone: BiddingZone) -> None:
    root, real, _ = placeholder_case(zone)
    flow = real.find("{*}flowDirection.direction")
    assert flow is not None
    flow.text = "A02"
    result = fetch_placeholder_case(ET.tostring(root), zone)
    assert len(result) == 192
    assert all(
        r.price_eur_per_mw == r.volume_mw == 0.0 for r in result if r.direction is Direction.UP
    )
    assert any(r.volume_mw != 0 for r in result if r.direction is Direction.DOWN)


@pytest.mark.parametrize("boundary", ["start", "end"])
def test_entsoe_placeholder_needs_exact_period(boundary: str) -> None:
    root, real, _ = placeholder_case(BiddingZone.NO1)
    stamp = real.find(f"{{*}}Period/{{*}}timeInterval/{{*}}{boundary}")
    assert stamp is not None
    stamp.text = (
        PLACEHOLDER_START - HOUR if boundary == "start" else PLACEHOLDER_END + HOUR
    ).isoformat()
    with pytest.raises(SourceError, match="duplicate capacity with differing values for mFRR"):
        fetch_placeholder_case(ET.tostring(root), BiddingZone.NO1)


def test_entsoe_placeholder_needs_another_series() -> None:
    root, real, placeholder = placeholder_case(BiddingZone.NO1)
    period = placeholder.find("{*}Period")
    assert period is not None
    real.append(period)
    root.remove(placeholder)
    with pytest.raises(SourceError, match="duplicate capacity with differing values for mFRR"):
        fetch_placeholder_case(ET.tostring(root), BiddingZone.NO1)


def test_entsoe_placeholder_filter_does_not_cross_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe, "_CHUNK", 12 * HOUR)
    root, real, placeholder = placeholder_case(BiddingZone.NO1)
    root.remove(real)
    first = ET.tostring(root)
    root.remove(placeholder)
    root.append(real)
    second = ET.tostring(root)

    def handler(request: httpx.Request) -> httpx.Response:
        content = first if request.url.params["periodStart"] == "202506042200" else second
        return httpx.Response(200, content=content)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        result = client.capacity_market(
            ReserveProduct.MFRR, PLACEHOLDER_START, PLACEHOLDER_END, zones=[BiddingZone.NO1]
        )
    assert len(result) == 96
    assert all(
        r.price_eur_per_mw == r.volume_mw == 0.0
        for r in result
        if r.start < PLACEHOLDER_START + 12 * HOUR
    )
    assert all(r.volume_mw != 0 for r in result if r.start >= PLACEHOLDER_START + 12 * HOUR)


@pytest.mark.parametrize("malformation", ["duplicate_position", "position_two", "currency"])
def test_entsoe_placeholder_is_validated_before_filtering(malformation: str) -> None:
    root, _, placeholder = placeholder_case(BiddingZone.NO1)
    if malformation == "currency":
        currency = placeholder.find("{*}currency_Unit.name")
        assert currency is not None
        currency.text = "NOK"
    else:
        period = placeholder.find("{*}Period")
        assert period is not None
        point = period.find("{*}Point")
        assert point is not None
        if malformation == "duplicate_position":
            period.append(ET.fromstring(ET.tostring(point)))
        else:
            position = point.find("{*}position")
            assert position is not None
            position.text = "2"
    with pytest.raises(SourceError):
        fetch_placeholder_case(ET.tostring(root), BiddingZone.NO1)


def test_entsoe_fcr_zero_placeholder_stays_strict() -> None:
    root = ET.fromstring((FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes())
    real = root.find("{*}TimeSeries")
    assert real is not None
    placeholder = ET.fromstring(ET.tostring(real))
    period = placeholder.find("{*}Period")
    assert period is not None
    points = period.findall("{*}Point")
    for point in points[1:]:
        period.remove(point)
    for field in ("quantity", "procurement_Price.amount"):
        node = points[0].find(f"{{*}}{field}")
        assert node is not None
        node.text = "0"
    root.append(placeholder)
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    with pytest.raises(SourceError) as exc:
        entsoe_fetch(
            ET.tostring(root), start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N
        )
    assert str(exc.value) == f"entsoe: duplicate capacity for NO1 at {start}"


def instance_limit_ack(prefix: str = "") -> bytes:
    root = ET.fromstring((FIXTURES.parent / "entsoe" / "acknowledgement-no-data.xml").read_bytes())
    text = root.find("{*}Reason/{*}text")
    assert text is not None
    text.text = prefix + "The number of instances (106) exceeds the allowed maximum (100)"
    return ET.tostring(root)


def request_window(request: httpx.Request) -> tuple[datetime, datetime]:
    return (
        datetime.strptime(request.url.params["periodStart"], "%Y%m%d%H%M").replace(tzinfo=UTC),
        datetime.strptime(request.url.params["periodEnd"], "%Y%m%d%H%M").replace(tzinfo=UTC),
    )


@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.MFRR])
@pytest.mark.parametrize("ack_status", [200, 400])
@pytest.mark.parametrize("trim", [False, True])
def test_entsoe_capacity_splits_instance_limit_without_changing_records(
    product: ReserveProduct,
    ack_status: int,
    trim: bool,
) -> None:
    if product is ReserveProduct.FCR_N:
        start = datetime(2026, 10, 5, 22, tzinfo=UTC)
        end = start + 3 * HOUR
        content = (FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
        maximum = HOUR
    else:
        start, end, content, maximum = START, END, archive(), 6 * HOUR
    query_start = start + INTERVAL + timedelta(microseconds=1) if trim else start
    query_end = end - INTERVAL + timedelta(microseconds=1) if trim else end
    expected = entsoe_fetch(content, start=query_start, end=query_end, product=product)
    calls: list[tuple[datetime, datetime]] = []
    # The limit phrase lies after the normal 200-character error truncation.
    acknowledgement = instance_limit_ack("test-token " + "detail " * 40)

    def handler(request: httpx.Request) -> httpx.Response:
        window = request_window(request)
        calls.append(window)
        if window[1] - window[0] > maximum:
            return httpx.Response(ack_status, content=acknowledgement)
        # ENTSO-E returns the whole Period, including outside this smaller chunk.
        return httpx.Response(200, content=content)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        result = client.capacity_market(product, query_start, query_end, zones=[BiddingZone.NO1])
    assert result == expected
    assert [r.raw for r in result] == [r.raw for r in expected]
    assert len(calls) == (5 if product is ReserveProduct.FCR_N else 7)
    assert calls[0] == (start, end)
    assert all(a.minute == b.minute == 0 and a < b for a, b in calls)
    if product is ReserveProduct.FCR_N:
        assert calls == [
            (start, end),
            (start, start + 2 * HOUR),
            (start, start + HOUR),
            (start + HOUR, start + 2 * HOUR),
            (start + 2 * HOUR, end),
        ]


@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.MFRR])
def test_entsoe_capacity_instance_limit_at_one_hour_raises(product: ReserveProduct) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, content=instance_limit_ack("test-token "))

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
        pytest.raises(SourceError) as exc,
    ):
        client.capacity_market(product, START, START + HOUR, zones=[BiddingZone.NO1])
    assert len(calls) == 1
    assert type(exc.value) is SourceError
    assert "exceeds the allowed maximum" in str(exc.value)
    assert "[REDACTED_SECRET]" in str(exc.value) and "test-token" not in str(exc.value)


@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.MFRR])
def test_entsoe_capacity_default_chunks_are_seven_days(product: ReserveProduct) -> None:
    calls: list[tuple[datetime, datetime]] = []
    no_data = (FIXTURES.parent / "entsoe" / "acknowledgement-no-data.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request_window(request))
        return httpx.Response(200, content=no_data)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        result = client.capacity_market(
            product, START, START + timedelta(days=30), zones=[BiddingZone.NO1]
        )
    assert result == []
    assert len(calls) == 5
    assert calls == [
        (START + timedelta(days=offset), START + timedelta(days=min(offset + 7, 30)))
        for offset in (0, 7, 14, 21, 28)
    ]


@pytest.mark.parametrize("days,expected_requests", [(30, 1), (367, 2)])
def test_entsoe_imbalance_keeps_year_chunks(days: int, expected_requests: int) -> None:
    calls: list[tuple[datetime, datetime]] = []
    no_data = (FIXTURES.parent / "entsoe" / "acknowledgement-no-data.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request_window(request))
        assert request.url.params["documentType"] == "A85"
        return httpx.Response(200, content=no_data)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        assert client.imbalance_prices(START, START + timedelta(days=days), [BiddingZone.NO1]) == []
    assert len(calls) == expected_requests
    assert calls[0][1] - calls[0][0] == timedelta(days=min(days, 366))


@pytest.mark.parametrize("kind", ["other_ack", "http_error", "invalid_xml"])
def test_entsoe_capacity_only_splits_instance_limit_acknowledgements(kind: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if kind == "http_error":
            return httpx.Response(404, text="exceeds the allowed maximum")
        if kind == "invalid_xml":
            return httpx.Response(200, content=b"not XML: exceeds the allowed maximum")
        root = ET.fromstring(instance_limit_ack())
        text = root.find("{*}Reason/{*}text")
        assert text is not None
        text.text = "permission denied"
        return httpx.Response(400, content=ET.tostring(root))

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
        pytest.raises(SourceError),
    ):
        client.capacity_market(ReserveProduct.MFRR, START, END, zones=[BiddingZone.NO1])
    assert len(calls) == 1


def test_entsoe_imbalance_does_not_split_instance_limit_acknowledgements() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, content=instance_limit_ack())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
        pytest.raises(SourceError, match="exceeds the allowed maximum"),
    ):
        client.imbalance_prices(START, END, [BiddingZone.NO1])
    assert len(calls) == 1


@pytest.mark.parametrize("where", ["series", "document"])
@pytest.mark.parametrize("field", ["quantity", "procurement_Price.amount"])
def test_entsoe_fcr_differing_repeat_values_still_raise(where: str, field: str) -> None:
    xml = (FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
    root = ET.fromstring(xml)
    series = root.find("{*}TimeSeries")
    assert series is not None
    changed = ET.fromstring(ET.tostring(series))
    node = changed.find(f"{{*}}Period/{{*}}Point/{{*}}{field}")
    assert node is not None
    node.text = "999.25"
    if where == "series":
        root.append(changed)
        content = ET.tostring(root)
    else:
        root.remove(series)
        root.append(changed)
        content = zip_members(xml, ET.tostring(root))
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    with pytest.raises(SourceError) as exc:
        entsoe_fetch(content, start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N)
    assert str(exc.value) == f"entsoe: duplicate capacity for NO1 at {start}"


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_mfrr_point_flow_conflicting_with_series_raises(zone: BiddingZone) -> None:
    root = ET.fromstring(xml_member(zone))
    namespace = root.tag.split("}")[0] + "}"
    series = root.find(f"{namespace}TimeSeries")
    assert series is not None
    series_flow = series.findtext(f"{namespace}flowDirection.direction")
    point = series.find(f"{namespace}Period/{namespace}Point")
    assert point is not None and series_flow in ("A01", "A02")
    flow = ET.SubElement(point, f"{namespace}flowDirection.direction")
    flow.text = "A02" if series_flow == "A01" else "A01"
    with pytest.raises(SourceError, match=r"mFRR capacity flowDirection.direction conflicts"):
        entsoe_fetch(ET.tostring(root), zone=zone)
