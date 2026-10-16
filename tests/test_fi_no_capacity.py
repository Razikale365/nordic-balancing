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
        entsoe_fetch(zip_members(first, ET.tostring(root)))


def test_entsoe_fcr_identical_documents_collapse() -> None:
    xml = (FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
    result = entsoe_fetch(
        zip_members(xml, xml),
        start=datetime(2026, 10, 5, 22, tzinfo=UTC),
        end=datetime(2026, 10, 6, 1, tzinfo=UTC),
        product=ReserveProduct.FCR_N,
    )
    assert len(result) == 12
    assert all(r.direction is Direction.NONE for r in result)
    with pytest.raises(SourceError, match="duplicate capacity"):
        entsoe_fetch(
            zip_members(xml, xml.replace(b"<quantity>14.4", b"<quantity>15.4", 1)),
            start=datetime(2026, 10, 5, 22, tzinfo=UTC),
            end=datetime(2026, 10, 6, 1, tzinfo=UTC),
            product=ReserveProduct.FCR_N,
        )


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
def test_entsoe_rejects_duplicate_series_in_one_document(zone: BiddingZone) -> None:
    root = ET.fromstring(xml_member(zone))
    series = root.find("{*}TimeSeries")
    assert series is not None
    root.append(ET.fromstring(ET.tostring(series)))
    with pytest.raises(SourceError, match="duplicate capacity"):
        entsoe_fetch(ET.tostring(root), zone=zone)


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
