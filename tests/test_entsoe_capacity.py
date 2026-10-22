"""ENTSO-E reserve capacity (A81): FCR, aFRR and mFRR documents, placeholders and chunking."""

import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest
from fakes import (
    FI_NO_END,
    FI_NO_FIXTURES,
    FI_NO_START,
    NS,
    entsoe_client,
    fi_no_archive,
    fi_no_entsoe_fetch,
    fi_no_fingrid_fetch,
    fi_no_xml_member,
    http_client,
    zip_members,
)

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EntsoeClient,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
)
from nordic_balancing.sources import entsoe

FCR_START = datetime(2026, 10, 5, 22, tzinfo=UTC)


def capacity_document(*points: str) -> str:
    return (
        f'<Balancing_MarketDocument xmlns="{NS}"><type>A81</type>'
        "<process.processType>A52</process.processType>"
        "<TimeSeries><businessType>B95</businessType>"
        "<flowDirection.direction>A03</flowDirection.direction>"
        "<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>"
        "<currency_Unit.name>EUR</currency_Unit.name>"
        "<curveType>A03</curveType>"
        "<Period><timeInterval><start>2026-10-05T22:00Z</start>"
        "<end>2026-10-06T01:00Z</end></timeInterval>"
        "<resolution>PT60M</resolution>"
        f"{''.join(points)}</Period></TimeSeries></Balancing_MarketDocument>"
    )


def capacity_point(position: Any, head: str = "") -> str:
    return (
        f"<Point><position>{position}</position><quantity>14.4</quantity>"
        f"<procurement_Price.amount>17.2</procurement_Price.amount>{head}</Point>"
    )


def capacity_prices(body: str) -> list[Any]:
    return entsoe_client(lambda _: httpx.Response(200, text=body)).capacity_market(
        ReserveProduct.FCR_N,
        FCR_START,
        FCR_START + timedelta(hours=3),
        zones=(BiddingZone.NO1,),
    )


FCR_FIXTURES = Path(__file__).parent / "fixtures" / "fcr"
HOUR = timedelta(hours=1)


def entsoe_fcr_fetch(
    xml: bytes | None = None,
    *,
    start: datetime = FCR_START,
    end: datetime = FCR_START + 3 * HOUR,
) -> list[ReserveCapacity]:
    if xml is None:
        xml = (FCR_FIXTURES / "entsoe-no1.xml").read_bytes()
    with (
        http_client(lambda _: httpx.Response(200, content=xml)) as http,
        EntsoeClient("test-token", http) as client,
    ):
        return client.capacity_market(ReserveProduct.FCR_N, start, end, zones=(BiddingZone.NO1,))


def delivery_day_xml() -> bytes:
    # Keep the sample's 22:00Z boundary and extend its trimmed Period to a day.
    # Positions 1 (22:00Z) and 5 (02:00Z) leave midnight implied by the first point.
    return (
        (FCR_FIXTURES / "entsoe-no1.xml")
        .read_bytes()
        .replace(b"2026-10-06T01:00Z", b"2026-10-06T22:00Z")
        .replace(b"<position>3</position>", b"<position>5</position>")
        .replace(b"<quantity>14.4</quantity>", b"<quantity>15.4</quantity>", 1)
    )


EARLY_DUPLICATE_FIXTURES = [
    (BiddingZone.NO1, "2024-02-15", datetime(2024, 2, 14, 23, tzinfo=UTC)),
    (BiddingZone.FI, "2024-11-13", datetime(2024, 11, 12, 23, tzinfo=UTC)),
]


def early_archive(zone: BiddingZone, delivery_day: str) -> bytes:
    return (
        FI_NO_FIXTURES / f"entsoe-a81-a47-{zone.value.lower()}-{delivery_day}-duplicated-series.zip"
    ).read_bytes()


def early_document(zone: BiddingZone, delivery_day: str) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(early_archive(zone, delivery_day))) as fi_no_archive:
        assert len(fi_no_archive.namelist()) == 1
        return ET.fromstring(fi_no_archive.read(fi_no_archive.namelist()[0]))


PLACEHOLDER_START = datetime(2025, 6, 4, 22, tzinfo=UTC)
PLACEHOLDER_END = PLACEHOLDER_START + 24 * HOUR


def placeholder_archive(zone: BiddingZone) -> bytes:
    return (
        FI_NO_FIXTURES / f"entsoe-a81-a47-{zone.value.lower()}-2025-06-05-placeholder-series.zip"
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
    return fi_no_entsoe_fetch(content, zone=zone, start=PLACEHOLDER_START, end=PLACEHOLDER_END)


def instance_limit_ack(prefix: str = "") -> bytes:
    root = ET.fromstring(
        (FI_NO_FIXTURES.parent / "entsoe" / "acknowledgement-no-data.xml").read_bytes()
    )
    text = root.find("{*}Reason/{*}text")
    assert text is not None
    text.text = prefix + "The number of instances (106) exceeds the allowed maximum (100)"
    return ET.tostring(root)


def request_window(request: httpx.Request) -> tuple[datetime, datetime]:
    return (
        datetime.strptime(request.url.params["periodStart"], "%Y%m%d%H%M").replace(tzinfo=UTC),
        datetime.strptime(request.url.params["periodEnd"], "%Y%m%d%H%M").replace(tzinfo=UTC),
    )


LIMIT_TEXT = "The number of instances (106) exceeds the allowed maximum (100)"


def mixed_limit_ack(reasons: list[list[str]]) -> bytes:
    root = ET.fromstring(
        (FI_NO_FIXTURES.parent / "entsoe" / "acknowledgement-no-data.xml").read_bytes()
    )
    namespace = root.tag.split("}")[0] + "}"
    original = root.find(f"{namespace}Reason")
    assert original is not None
    root.remove(original)
    for texts in reasons:
        reason = ET.SubElement(root, f"{namespace}Reason")
        for text in texts:
            element = ET.SubElement(reason, f"{namespace}text")
            element.text = text
    return ET.tostring(root)


# --- Instance limit and chunking ---------------------------------------------------------------


def test_entsoe_capacity_chunks_and_deduplicates_zone_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe, "_CHUNK", 3 * HOUR)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        xml = (FCR_FIXTURES / "entsoe-no1.xml").read_text()
        if request.url.params["periodStart"] == "202610060100":
            xml = xml.replace("2026-10-06T01:00Z", "2026-10-06T04:00Z")
            xml = xml.replace("2026-10-05T22:00Z", "2026-10-06T01:00Z")
        return httpx.Response(200, content=xml.encode())

    with http_client(handler) as http:
        result = EntsoeClient("test-token", http, max_workers=1).capacity_market(
            ReserveProduct.FCR_N,
            FCR_START,
            FCR_START + 6 * HOUR,
            zones=(z for z in (BiddingZone.NO1, BiddingZone.NO1)),
        )
    assert len(seen) == 2
    assert len(result) == 24
    assert len({(r.zone, r.start) for r in result}) == 24


def test_entsoe_capacity_two_chunks_clip_same_delivery_day_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe, "_CHUNK", 12 * HOUR)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=delivery_day_xml())

    with http_client(handler) as http:
        records = EntsoeClient("test-token", http, max_workers=1).capacity_market(
            ReserveProduct.FCR_N,
            FCR_START,
            FCR_START + 24 * HOUR,
            zones=(BiddingZone.NO1,),
        )
    assert len(seen) == 2
    assert {r.url.params["periodStart"] for r in seen} == {"202610052200", "202610061000"}
    assert [r.start for r in records] == [FCR_START + q * INTERVAL for q in range(96)]
    assert len({(r.zone, r.start) for r in records}) == 96
    assert [r.price_eur_per_mw for r in records] == [17.2] * 16 + [18.9] * 80
    assert [r.volume_mw for r in records] == [15.4] * 16 + [14.4] * 80


def test_entsoe_whole_day_clips_across_two_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entsoe, "_CHUNK", 12 * HOUR)
    seen: list[httpx.Request] = []
    chunked = fi_no_entsoe_fetch(seen=seen)
    monkeypatch.setattr(entsoe, "_CHUNK", timedelta(days=366))
    assert chunked == fi_no_entsoe_fetch()
    assert len(seen) == 2
    assert [r.url.params["periodStart"] for r in seen] == ["202610072200", "202610081000"]
    clipped = fi_no_entsoe_fetch(
        start=FI_NO_START + INTERVAL + timedelta(microseconds=1), end=FI_NO_END - INTERVAL
    )
    assert len(clipped) == 93 * 2
    assert all(FI_NO_START + INTERVAL < r.start < FI_NO_END - INTERVAL for r in clipped)


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
        content = (FI_NO_FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
        maximum = HOUR
    else:
        start, end, content, maximum = FI_NO_START, FI_NO_END, fi_no_archive(), 6 * HOUR
    query_start = start + INTERVAL + timedelta(microseconds=1) if trim else start
    query_end = end - INTERVAL + timedelta(microseconds=1) if trim else end
    expected = fi_no_entsoe_fetch(content, start=query_start, end=query_end, product=product)
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
        http_client(handler) as http,
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


# The limit phrase only needs to appear once: other texts may sit beside it
# in the same Reason or in another Reason, in either order.
@pytest.mark.parametrize(
    "reasons",
    [
        [[LIMIT_TEXT, "Details: reduce the interval"]],
        [["Details: reduce the interval", LIMIT_TEXT]],
        [[LIMIT_TEXT], ["Details: reduce the interval"]],
        [["Details: reduce the interval"], [LIMIT_TEXT]],
    ],
    ids=[
        "one-reason-limit-first",
        "one-reason-limit-second",
        "two-reasons-limit-first",
        "two-reasons-limit-second",
    ],
)
def test_entsoe_capacity_splits_mixed_instance_limit_acknowledgements(
    reasons: list[list[str]],
) -> None:
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    end = start + 3 * HOUR
    content = (FI_NO_FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
    expected = fi_no_entsoe_fetch(content, start=start, end=end, product=ReserveProduct.FCR_N)
    acknowledgement = mixed_limit_ack(reasons)
    calls: list[tuple[datetime, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        window = request_window(request)
        calls.append(window)
        if window[1] - window[0] > HOUR:
            return httpx.Response(400, content=acknowledgement)
        return httpx.Response(200, content=content)

    with (
        http_client(handler) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        result = client.capacity_market(ReserveProduct.FCR_N, start, end, zones=[BiddingZone.NO1])
    assert result == expected
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
        http_client(handler) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
        pytest.raises(SourceError) as exc,
    ):
        client.capacity_market(product, FI_NO_START, FI_NO_START + HOUR, zones=[BiddingZone.NO1])
    assert len(calls) == 1
    assert type(exc.value) is SourceError
    assert "exceeds the allowed maximum" in str(exc.value)
    assert "[REDACTED_SECRET]" in str(exc.value) and "test-token" not in str(exc.value)


@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.MFRR])
def test_entsoe_capacity_default_chunks_are_seven_days(product: ReserveProduct) -> None:
    calls: list[tuple[datetime, datetime]] = []
    no_data = (FI_NO_FIXTURES.parent / "entsoe" / "acknowledgement-no-data.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request_window(request))
        return httpx.Response(200, content=no_data)

    with (
        http_client(handler) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        result = client.capacity_market(
            product, FI_NO_START, FI_NO_START + timedelta(days=30), zones=[BiddingZone.NO1]
        )
    assert result == []
    assert len(calls) == 5
    assert calls == [
        (FI_NO_START + timedelta(days=offset), FI_NO_START + timedelta(days=min(offset + 7, 30)))
        for offset in (0, 7, 14, 21, 28)
    ]


@pytest.mark.parametrize("days,expected_requests", [(30, 1), (367, 2)])
def test_entsoe_imbalance_keeps_year_chunks(days: int, expected_requests: int) -> None:
    calls: list[tuple[datetime, datetime]] = []
    no_data = (FI_NO_FIXTURES.parent / "entsoe" / "acknowledgement-no-data.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request_window(request))
        assert request.url.params["documentType"] == "A85"
        return httpx.Response(200, content=no_data)

    with (
        http_client(handler) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        assert (
            client.imbalance_prices(
                FI_NO_START, FI_NO_START + timedelta(days=days), [BiddingZone.NO1]
            )
            == []
        )
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

    expected = {
        "other_ack": "acknowledgement: permission denied",
        "http_error": "HTTP 404",
        "invalid_xml": "document is not XML",
    }[kind]
    with (
        http_client(handler) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
        pytest.raises(SourceError, match=expected),
    ):
        client.capacity_market(ReserveProduct.MFRR, FI_NO_START, FI_NO_END, zones=[BiddingZone.NO1])
    assert len(calls) == 1


def test_entsoe_imbalance_does_not_split_instance_limit_acknowledgements() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, content=instance_limit_ack())

    with (
        http_client(handler) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
        pytest.raises(SourceError, match="exceeds the allowed maximum"),
    ):
        client.imbalance_prices(FI_NO_START, FI_NO_END, [BiddingZone.NO1])
    assert len(calls) == 1


# --- Zero placeholder series -------------------------------------------------------------------


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
        http_client(handler) as http,
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
    expected = {
        "duplicate_position": "duplicate capacity position",
        "position_two": "missing position 1",
        "currency": "currency_Unit.name must be EUR",
    }[malformation]
    with pytest.raises(SourceError, match=expected):
        fetch_placeholder_case(ET.tostring(root), BiddingZone.NO1)


def test_entsoe_fcr_zero_placeholder_stays_strict() -> None:
    root = ET.fromstring((FI_NO_FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes())
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
        fi_no_entsoe_fetch(
            ET.tostring(root), start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N
        )
    assert str(exc.value) == f"entsoe: duplicate capacity for NO1 at {start}"


# The placeholder exception is not restricted to A03 Periods: a one-hour
# zero/zero A01 series is ignored beside a complete A01 series covering the
# same direction and Period, and the output is unchanged without it.
def test_entsoe_a01_placeholder_is_ignored_beside_a_complete_a01_series() -> None:
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    root = ET.fromstring((FI_NO_FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes())
    process = root.find("{*}process.processType")
    assert process is not None
    process.text = "A47"
    series = root.find("{*}TimeSeries")
    assert series is not None
    for tag, value in (("flowDirection.direction", "A01"), ("curveType", "A01")):
        node = series.find(f"{{*}}{tag}")
        assert node is not None
        node.text = value
    period = series.find("{*}Period")
    assert period is not None
    end = period.find("{*}timeInterval/{*}end")
    assert end is not None
    end.text = (start + HOUR).isoformat()
    for point in period.findall("{*}Point")[1:]:
        period.remove(point)
    placeholder = ET.fromstring(ET.tostring(series))
    for tag in ("quantity", "procurement_Price.amount"):
        node = placeholder.find(f"{{*}}Period/{{*}}Point/{{*}}{tag}")
        assert node is not None
        node.text = "0"
    root.append(placeholder)
    with_placeholder = fi_no_entsoe_fetch(ET.tostring(root), start=start, end=start + HOUR)
    root.remove(placeholder)
    assert with_placeholder == fi_no_entsoe_fetch(ET.tostring(root), start=start, end=start + HOUR)
    assert len(with_placeholder) == 4
    assert all(r.price_eur_per_mw == 17.2 for r in with_placeholder)
    assert all(r.volume_mw == 14.4 for r in with_placeholder)


# --- Duplicate and repeated series -------------------------------------------------------------


# quantity is a singleton in a capacity Point, and Python numeric spellings are
# not valid xs:decimal/xs:positiveInteger lexical forms.
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "<quantity>14.4</quantity>",
            "<quantity>14.4</quantity><quantity>15.4</quantity>",
            "more than one quantity in Point",
        ),
        ("<position>1</position>", "<position>1_0</position>", "unparseable position"),
        ("<quantity>14.4</quantity>", "<quantity>1_0.5</quantity>", "unparseable quantity"),
        (
            "<procurement_Price.amount>17.2</procurement_Price.amount>",
            "<procurement_Price.amount>1e5</procurement_Price.amount>",
            "unparseable procurement_Price.amount",
        ),
    ],
)
def test_capacity_rejects_duplicated_singletons_and_python_literals(
    old: str, new: str, message: str
) -> None:
    body = capacity_document(capacity_point(1).replace(old, new))
    with pytest.raises(SourceError, match=message):
        capacity_prices(body)


@pytest.mark.parametrize("kind", ["series", "document", "point"])
def test_entsoe_capacity_collapses_identical_repeats_except_period_positions(kind: str) -> None:
    xml = (FCR_FIXTURES / "entsoe-no1.xml").read_text()
    expected = entsoe_fcr_fetch(xml.encode())
    if kind == "series":
        a, b = xml.index("<TimeSeries>"), xml.index("</TimeSeries>") + len("</TimeSeries>")
        xml = xml[:b] + xml[a:b] + xml[b:]
        content = xml.encode()
    elif kind == "point":
        a, b = xml.index("<Point>"), xml.index("</Point>") + len("</Point>")
        xml = xml[:b] + xml[a:b] + xml[b:]
        with pytest.raises(SourceError, match="duplicate capacity position"):
            entsoe_fcr_fetch(xml.encode())
        return
    else:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("first.xml", xml)
            output.writestr("second.xml", xml)
        content = archive.getvalue()
    result = entsoe_fcr_fetch(content)
    assert len(result) == 12
    assert result == expected
    assert [r.raw for r in result] == [r.raw for r in expected]


@pytest.mark.parametrize("position", [1, 5])
def test_entsoe_capacity_rejects_duplicate_points_outside_window(position: int) -> None:
    xml = delivery_day_xml()
    marker = f"<position>{position}</position>".encode()
    a = xml.rindex(b"<Point>", 0, xml.index(marker))
    b = xml.index(b"</Point>", a) + len(b"</Point>")
    xml = xml[:b] + xml[a:b] + xml[b:]
    with pytest.raises(SourceError, match="duplicate capacity position"):
        entsoe_fcr_fetch(xml, start=FCR_START + 2 * HOUR, end=FCR_START + 3 * HOUR)


@pytest.mark.parametrize("field", ["quantity", "procurement_Price.amount"])
def test_entsoe_conflicting_document_still_raises(field: str) -> None:
    first = fi_no_xml_member()
    root = ET.fromstring(first)
    node = root.find(f"{{*}}TimeSeries/{{*}}Period/{{*}}Point/{{*}}{field}")
    assert node is not None
    node.text = "999.25"
    with pytest.raises(SourceError, match="duplicate capacity"):
        fi_no_entsoe_fetch(zip_members(first, ET.tostring(root)), product=ReserveProduct.MFRR)


def test_entsoe_mfrr_identical_documents_collapse() -> None:
    xml = fi_no_xml_member()
    result = fi_no_entsoe_fetch(zip_members(xml, xml), product=ReserveProduct.MFRR)
    assert len(result) == 192
    assert all(r.product is ReserveProduct.MFRR for r in result)
    assert result == fi_no_entsoe_fetch(product=ReserveProduct.MFRR)


@pytest.mark.parametrize("where", ["series", "document"])
def test_entsoe_fcr_identical_repeats_collapse(where: str) -> None:
    xml = (FI_NO_FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
    if where == "series":
        root = ET.fromstring(xml)
        series = root.find("{*}TimeSeries")
        assert series is not None
        root.append(ET.fromstring(ET.tostring(series)))
        content = ET.tostring(root)
    else:
        content = zip_members(xml, xml)
    start = datetime(2026, 10, 5, 22, tzinfo=UTC)
    result = fi_no_entsoe_fetch(
        content, start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N
    )
    expected = fi_no_entsoe_fetch(
        xml, start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N
    )
    assert len(result) == 12
    assert result == expected
    assert [r.raw for r in result] == [r.raw for r in expected]


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_mfrr_identical_series_in_one_document_collapse(zone: BiddingZone) -> None:
    root = ET.fromstring(fi_no_xml_member(zone))
    series = root.find("{*}TimeSeries")
    assert series is not None
    root.append(ET.fromstring(ET.tostring(series)))
    result = fi_no_entsoe_fetch(ET.tostring(root), zone=zone)
    assert len(result) == 192
    assert result == fi_no_entsoe_fetch(fi_no_xml_member(zone), zone=zone)


@pytest.mark.parametrize("zone,delivery_day,start", EARLY_DUPLICATE_FIXTURES)
def test_entsoe_historical_identical_series_exact_values(
    zone: BiddingZone,
    delivery_day: str,
    start: datetime,
) -> None:
    result = fi_no_entsoe_fetch(
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
        fi_no_entsoe_fetch(ET.tostring(root), zone=zone, start=start, end=start + 24 * HOUR)


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
        fi_no_entsoe_fetch(ET.tostring(root), zone=zone, start=start, end=start + 24 * HOUR)
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
    result = fi_no_entsoe_fetch(ET.tostring(root), zone=zone, start=start, end=start + 24 * HOUR)
    assert len(result) == 192
    assert result == fi_no_entsoe_fetch(
        early_archive(zone, delivery_day), zone=zone, start=start, end=start + 24 * HOUR
    )


@pytest.mark.parametrize("where", ["series", "document"])
def test_entsoe_fcr_conflicting_duplicate_error_text_unchanged(where: str) -> None:
    xml = (FI_NO_FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
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
        fi_no_entsoe_fetch(content, start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N)
    assert str(exc.value) == f"entsoe: duplicate capacity for NO1 at {start}"


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


@pytest.mark.parametrize("where", ["series", "document"])
@pytest.mark.parametrize("field", ["quantity", "procurement_Price.amount"])
def test_entsoe_fcr_differing_repeat_values_still_raise(where: str, field: str) -> None:
    xml = (FI_NO_FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes()
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
        fi_no_entsoe_fetch(content, start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N)
    assert str(exc.value) == f"entsoe: duplicate capacity for NO1 at {start}"


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_mfrr_point_flow_conflicting_with_series_raises(zone: BiddingZone) -> None:
    root = ET.fromstring(fi_no_xml_member(zone))
    namespace = root.tag.split("}")[0] + "}"
    series = root.find(f"{namespace}TimeSeries")
    assert series is not None
    series_flow = series.findtext(f"{namespace}flowDirection.direction")
    point = series.find(f"{namespace}Period/{namespace}Point")
    assert point is not None and series_flow in ("A01", "A02")
    flow = ET.SubElement(point, f"{namespace}flowDirection.direction")
    flow.text = "A02" if series_flow == "A01" else "A01"
    with pytest.raises(SourceError, match=r"mFRR capacity flowDirection.direction conflicts"):
        fi_no_entsoe_fetch(ET.tostring(root), zone=zone)


# --- Arguments and client lifecycle ------------------------------------------------------------


@pytest.mark.parametrize(
    "product",
    [ReserveProduct.AFRR, ReserveProduct.FCR_D, ReserveProduct.FCR, "FCR-N"],
)
def test_entsoe_rejects_other_products(product: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError, match="FCR_N"):
        client.capacity_market(product, FCR_START, FCR_START + HOUR)


@pytest.mark.parametrize("zones", [(BiddingZone.DK2,), (BiddingZone.SE3,), (), ("NO1",)])
def test_entsoe_rejects_invalid_zones(zones: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError):
        client.capacity_market(ReserveProduct.FCR_N, FCR_START, FCR_START + HOUR, zones=zones)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (FCR_START, FCR_START),
        (FCR_START, FCR_START - HOUR),
        (FCR_START.replace(tzinfo=None), FCR_START + HOUR),
        (FCR_START, (FCR_START + HOUR).replace(tzinfo=None)),
    ],
)
def test_entsoe_capacity_checks_times(start: datetime, end: datetime) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError):
        client.capacity_market(ReserveProduct.FCR_N, start, end)


@pytest.mark.parametrize(
    "product", [ReserveProduct.AFRR, ReserveProduct.FCR_D, ReserveProduct.FCR, "mFRR", None]
)
def test_entsoe_invalid_product(product: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError, match=r"FCR_N.*MFRR"):
        client.capacity_market(product, FI_NO_START, FI_NO_END)


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
        client.capacity_market(product, FI_NO_START, FI_NO_END, zones=[zone])


def test_entsoe_mfrr_empty_zones() -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError, match="empty"):
        client.capacity_market(ReserveProduct.MFRR, FI_NO_START, FI_NO_END, zones=[])


# --- Retries, transport errors and API key handling --------------------------------------------


def test_entsoe_capacity_acknowledgements_and_redaction() -> None:
    xml = (
        b"<Acknowledgement_MarketDocument><Reason><text>No matching data found</text>"
        b"</Reason></Acknowledgement_MarketDocument>"
    )
    assert entsoe_fcr_fetch(xml) == []
    with pytest.raises(SourceError, match="REDACTED_SECRET") as exc:
        entsoe_fcr_fetch(xml.replace(b"No matching data found", b"test-token invalid request"))
    assert "test-token" not in str(exc.value)


def test_entsoe_mfrr_error_redaction() -> None:
    xml = fi_no_xml_member().replace(
        b"<flowDirection.direction>A01", b"<flowDirection.direction>test-token"
    )
    with pytest.raises(SourceError) as exc:
        fi_no_entsoe_fetch(xml)
    assert "test-token" not in str(exc.value)
    xml = (
        b"<Acknowledgement_MarketDocument><Reason><text>test-token denied</text>"
        b"</Reason></Acknowledgement_MarketDocument>"
    )
    with pytest.raises(SourceError, match="REDACTED_SECRET") as exc:
        fi_no_entsoe_fetch(xml)
    assert "test-token" not in str(exc.value)


# --- Delivery day clipping and fill ------------------------------------------------------------


def test_entsoe_parses_a03_quantity_and_price_continuation_from_zip() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("no1.xml", (FCR_FIXTURES / "entsoe-no1.xml").read_bytes())
    result = entsoe_fcr_fetch(archive.getvalue())
    assert len(result) == 12
    assert [r.start for r in result] == [FCR_START + q * INTERVAL for q in range(12)]
    assert [r.price_eur_per_mw for r in result] == [17.2] * 8 + [18.9] * 4
    assert all(r.volume_mw == 14.4 for r in result)
    assert all(r.direction is Direction.NONE and r.zone is BiddingZone.NO1 for r in result)
    assert all(r.product is ReserveProduct.FCR_N and r.source == "entsoe" for r in result)
    assert all(r.resolution == HOUR for r in result)
    assert all(getattr(r, f.name) is not None for r in result for f in fields(ReserveCapacity))
    assert result[0].raw["quantity_Measure_Unit.name"] == "MAW"
    assert result[0].raw["currency_Unit.name"] == "EUR"
    xml = (
        (FCR_FIXTURES / "entsoe-no1.xml")
        .read_bytes()
        .replace(
            b"<quantity>14.4</quantity>",
            b"<quantity>15.4</quantity>",
            1,
        )
    )
    result = entsoe_fcr_fetch(xml)
    assert [r.volume_mw for r in result] == [15.4] * 8 + [14.4] * 4


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
    start, end = FCR_START + offset * HOUR, FCR_START + (offset + length) * HOUR
    records = entsoe_fcr_fetch(delivery_day_xml(), start=start, end=end)
    assert [r.start for r in records] == [start + q * INTERVAL for q in range(length * 4)]
    assert [r.price_eur_per_mw for r in records] == prices
    assert [r.volume_mw for r in records] == volumes
    assert all(r.resolution == HOUR for r in records)


@pytest.mark.parametrize("offset", [-2, 24])
def test_entsoe_capacity_accepts_non_overlapping_period(offset: int) -> None:
    start = FCR_START + offset * HOUR
    assert entsoe_fcr_fetch(delivery_day_xml(), start=start, end=start + HOUR) == []


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
    start = FCR_START + offset * HOUR
    with pytest.raises(SourceError, match=message):
        entsoe_fcr_fetch(xml, start=start, end=start + HOUR)


def test_entsoe_capacity_validates_a03_start_even_for_non_overlapping_period() -> None:
    xml = delivery_day_xml().replace(b"<position>1</position>", b"<position>2</position>")
    with pytest.raises(SourceError, match="missing position 1"):
        entsoe_fcr_fetch(xml, start=FCR_START + 25 * HOUR, end=FCR_START + 26 * HOUR)


def test_fi_mfrr_offline_cross_check_all_hours_and_directions() -> None:
    fg, en = fi_no_fingrid_fetch(ReserveProduct.MFRR), fi_no_entsoe_fetch(zone=BiddingZone.FI)
    assert len(fg) == len(en) == 192
    assert {(r.start, r.direction): (r.price_eur_per_mw, r.volume_mw) for r in fg} == {
        (r.start, r.direction): (r.price_eur_per_mw, r.volume_mw) for r in en
    }


def test_entsoe_a03_fills_both_quantity_and_price() -> None:
    result = fi_no_entsoe_fetch()
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
            and FI_NO_START + hour * HOUR <= r.start < FI_NO_START + (hour + 1) * HOUR
        ]
        assert len(records) == 4
        assert {(r.price_eur_per_mw, r.volume_mw) for r in records} == {(price, volume)}
    fi = fi_no_entsoe_fetch(zone=BiddingZone.FI)
    assert all(
        r.price_eur_per_mw == 1.5 and r.volume_mw == 700.0
        for r in fi
        if r.direction is Direction.DOWN and r.start < FI_NO_START + 3 * HOUR
    )


# --- Rejected metadata -------------------------------------------------------------------------


# point-level capacity flowDirection must also be A03.
def test_rejects_point_level_capacity_flow_direction_other_than_a03() -> None:
    body = capacity_document(
        capacity_point(1, head="<flowDirection.direction>A01</flowDirection.direction>")
    )
    with pytest.raises(SourceError, match=r"capacity flowDirection\.direction must be A03"):
        capacity_prices(body)


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
    xml = (FCR_FIXTURES / "entsoe-no1.xml").read_text().replace(old, new)
    with pytest.raises(SourceError, match=message):
        entsoe_fcr_fetch(xml.encode())


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

    xml = (FCR_FIXTURES / "entsoe-no1.xml").read_text()
    xml = re.sub(r"<" + re.escape(tag) + r">.*?</" + re.escape(tag) + r">", "", xml)
    with pytest.raises(SourceError, match="missing"):
        entsoe_fcr_fetch(xml.encode())


@pytest.mark.parametrize("element", ["price_Measure_Unit.name", "price_Measurement_Unit.name"])
def test_entsoe_capacity_optional_price_unit_is_mw(element: str) -> None:
    xml = (FCR_FIXTURES / "entsoe-no1.xml").read_text()
    xml = xml.replace("<curveType>", f"<{element}>MAW</{element}><curveType>")
    assert len(entsoe_fcr_fetch(xml.encode())) == 12
    with pytest.raises(SourceError, match="unsupported"):
        entsoe_fcr_fetch(xml.replace(f"<{element}>MAW", f"<{element}>MWH").encode())


@pytest.mark.parametrize(
    "old,new,message",
    [
        (b"<type>A81", b"<type>A85", "unexpected document type"),
        (b"<process.processType>A47", b"<process.processType>A52", "processType must be A47"),
        (b"<businessType>B95", b"<businessType>A96", "businessType must be B95"),
        (
            b"<flowDirection.direction>A01",
            b"<flowDirection.direction>A03",
            "flowDirection.direction must be",
        ),
        (
            b"<quantity_Measure_Unit.name>MAW",
            b"<quantity_Measure_Unit.name>MWH",
            "quantity_Measure_Unit.name must be MAW",
        ),
        (b"<currency_Unit.name>EUR", b"<currency_Unit.name>NOK", "currency_Unit.name must be EUR"),
        (b"<resolution>PT60M", b"<resolution>PT15M", "resolution must be PT60M"),
        (b"<quantity>95", b"<quantity>NaN", "quantity is not finite"),
        (
            b"<procurement_Price.amount>1.5",
            b"<procurement_Price.amount>inf",
            "procurement_Price.amount is not finite",
        ),
    ],
)
def test_entsoe_rejects_invalid_mfrr_metadata(old: bytes, new: bytes, message: str) -> None:
    with pytest.raises(SourceError, match=re.escape(message)):
        fi_no_entsoe_fetch(fi_no_xml_member().replace(old, new))


def test_entsoe_rejects_missing_mfrr_direction() -> None:
    root = ET.fromstring(fi_no_xml_member())
    series = root.find("{*}TimeSeries")
    assert series is not None
    flow = series.find("{*}flowDirection.direction")
    assert flow is not None
    series.remove(flow)
    with pytest.raises(SourceError, match="missing flowDirection"):
        fi_no_entsoe_fetch(ET.tostring(root))


@pytest.mark.parametrize("zone", [BiddingZone.FI, BiddingZone.SE3, "NO1"])
def test_entsoe_fcr_zone_error_text_unchanged(zone: Any) -> None:
    with EntsoeClient("test-token") as client, pytest.raises(ValueError) as exc:
        client.capacity_market(ReserveProduct.FCR_N, FI_NO_START, FI_NO_END, zones=[zone])
    assert str(exc.value) == "ENTSO-E FCR-N capacity covers only NO1, NO2, NO3, NO4 and NO5"


@pytest.mark.parametrize("where", ["series", "point"])
def test_entsoe_fcr_flow_error_text_unchanged(where: str) -> None:
    root = ET.fromstring((FI_NO_FIXTURES.parent / "fcr" / "entsoe-no1.xml").read_bytes())
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
        fi_no_entsoe_fetch(
            ET.tostring(root), start=start, end=start + 3 * HOUR, product=ReserveProduct.FCR_N
        )
    assert str(exc.value) == "entsoe: capacity flowDirection.direction must be A03"


# --- Parsing and fixtures ----------------------------------------------------------------------


# +, a bare fraction and a trailing dot stay valid in capacity Points.
def test_capacity_accepts_xsd_numeric_forms() -> None:
    body = capacity_document(
        capacity_point(1)
        .replace("<position>1</position>", "<position>+1</position>")
        .replace("<quantity>14.4</quantity>", "<quantity>.5</quantity>")
        .replace(
            "<procurement_Price.amount>17.2</procurement_Price.amount>",
            "<procurement_Price.amount>5.</procurement_Price.amount>",
        )
    )
    records = capacity_prices(body)
    assert [r.volume_mw for r in records] == [0.5] * 12
    assert [r.price_eur_per_mw for r in records] == [5.0] * 12


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
            (FCR_FIXTURES / "entsoe-no1.xml")
            .read_text()
            .replace(
                "10YNO-1--------2",
                params["controlArea_Domain"],
            )
        )
        return httpx.Response(200, content=xml.encode())

    local = timezone(timedelta(hours=2))
    with http_client(handler) as http:
        result = EntsoeClient("test-token", http).capacity_market(
            ReserveProduct.FCR_N,
            (FCR_START + INTERVAL + timedelta(microseconds=1)).astimezone(local),
            (FCR_START + 2 * HOUR + INTERVAL + timedelta(microseconds=1)).astimezone(local),
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
    assert min(r.start for r in result) == FCR_START + 2 * INTERVAL
    assert max(r.start for r in result) == FCR_START + 2 * HOUR + INTERVAL


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
            entsoe_fcr_fetch(xml)
    else:
        assert entsoe_fcr_fetch(xml)
    assert cancelled == [True]


@pytest.mark.parametrize("zone", [BiddingZone.NO1, BiddingZone.FI])
def test_entsoe_exact_fixture_fields_and_query(zone: BiddingZone) -> None:
    seen: list[httpx.Request] = []
    result = fi_no_entsoe_fetch(zone=zone, seen=seen)
    assert len(result) == 24 * 2 * 4
    assert {(r.start, r.zone, r.direction) for r in result} == {
        (FI_NO_START + q * INTERVAL, zone, d)
        for q in range(96)
        for d in (Direction.UP, Direction.DOWN)
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
                if r.start == FI_NO_START + quarter * INTERVAL and r.direction is direction
            )
            expected = ReserveCapacity(
                start=FI_NO_START + quarter * INTERVAL,
                zone=zone,
                product=ReserveProduct.MFRR,
                direction=direction,
                price_eur_per_mw=price,
                volume_mw=volume,
                source="entsoe",
                resolution=HOUR,
                raw={
                    "zone": zone.value,
                    "start": FI_NO_START.isoformat(),
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


@pytest.mark.parametrize(
    "zone", [BiddingZone.NO2, BiddingZone.NO3, BiddingZone.NO4, BiddingZone.NO5]
)
def test_entsoe_all_norwegian_mfrr_zones(zone: BiddingZone) -> None:
    xml = fi_no_xml_member().replace(b"10YNO-1--------2", entsoe._EIC_CODES[zone].encode())
    result = fi_no_entsoe_fetch(xml, zone=zone)
    assert len(result) == 192
    assert {r.zone for r in result} == {zone}


def test_entsoe_mfrr_sorts_zones_before_directions() -> None:
    responses = {
        "10YNO-1--------2": fi_no_archive(BiddingZone.NO1),
        "10YFI-1--------U": fi_no_archive(BiddingZone.FI),
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.params["controlArea_Domain"]
        requested.append(domain)
        assert request.url.params["processType"] == "A47"
        return httpx.Response(200, content=responses[domain])

    with (
        http_client(handler) as http,
        EntsoeClient("test-token", http, max_workers=1) as client,
    ):
        result = client.capacity_market(
            ReserveProduct.MFRR, FI_NO_START, FI_NO_END, zones=[BiddingZone.NO1, BiddingZone.FI]
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
        (FI_NO_START + quarter * INTERVAL, zone, direction)
        for quarter in range(4)
        for zone, direction in zone_directions
    ]
    keys = [(r.start, r.zone, r.direction) for r in result]
    assert keys[:8] == expected_first_hour[:8]
    # Two zones x two directions x four quarters give 16 records in the first hour.
    assert keys[:16] == expected_first_hour
    assert keys == sorted(keys)


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


def test_entsoe_capacity_period_far_longer_than_the_request_is_rejected() -> None:
    body = capacity_document(capacity_point(1)).replace("2026-10-06T01:00Z", "9999-12-31T23:00Z")
    with pytest.raises(SourceError, match="capacity Period is longer than the request"):
        capacity_prices(body)


def test_entsoe_capacity_period_of_a_delivery_day_still_parses() -> None:
    body = capacity_document(capacity_point(1)).replace("2026-10-06T01:00Z", "2026-10-06T23:00Z")
    assert capacity_prices(body)
