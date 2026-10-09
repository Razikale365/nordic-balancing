import io
import os
import traceback
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    EntsoeClient,
    RateLimitError,
    SourceError,
)
from nordic_balancing.sources import EntsoeClient as SourceEntsoeClient

KEY = "test-entsoe-token"
NS = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:0"
NS_ALT = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:2:1"
NS_45 = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:5"
NS_30 = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:3:0"
ACK_NS = "urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0"
START = datetime(2026, 1, 15, tzinfo=UTC)
END = START + timedelta(hours=1)
FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"
Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def point(position: Any, amount: Any = 21.0, category: str | None = None) -> str:
    cat = f"<imbalance_Price.category>{category}</imbalance_Price.category>" if category else ""
    return (
        f"<Point><position>{position}</position>"
        f"<imbalance_Price.amount>{amount}</imbalance_Price.amount>{cat}</Point>"
    )


def period(
    start: str,
    end: str,
    *points: str,
    resolution: str = "PT15M",
    interval: str | None = None,
) -> str:
    time_interval = (
        interval
        if interval is not None
        else f"<timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
    )
    return f"<Period>{time_interval}<resolution>{resolution}</resolution>{''.join(points)}</Period>"


def series(
    *periods: str,
    curve: str | None = "A03",
    currency: str | None = "EUR",
) -> str:
    unit = f"<currency_Unit.name>{currency}</currency_Unit.name>" if currency is not None else ""
    tag = f"<curveType>{curve}</curveType>" if curve is not None else ""
    return f"<TimeSeries>{unit}{tag}{''.join(periods)}</TimeSeries>"


def document(*series_: str, namespace: str = NS) -> str:
    return (
        f'<Balancing_MarketDocument xmlns="{namespace}">{"".join(series_)}'
        "</Balancing_MarketDocument>"
    )


def acknowledgement(text: str) -> str:
    return (
        f'<Acknowledgement_MarketDocument xmlns="{ACK_NS}">'
        f"<Reason><code>999</code><text>{text}</text></Reason>"
        "</Acknowledgement_MarketDocument>"
    )


def zipped(*members: str | bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, member in enumerate(members):
            archive.writestr(f"member{index}.xml", member)
    return buffer.getvalue()


ONE_HOUR = document(
    series(
        period(
            "2026-01-15T00:00Z",
            "2026-01-15T01:00Z",
            point(1, 21.0, "A04"),
            point(2, 21.5, "A04"),
            point(3, 22.0, "A05"),
            point(4, 22.5, "A05"),
        )
    )
)


def client(handler: Handler, sleeps: list[float] | None = None, **kwargs: Any) -> EntsoeClient:
    recorded = sleeps if sleeps is not None else []
    return EntsoeClient(
        KEY,
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=recorded.append,
        **kwargs,
    )


def test_parses_quarter_hour_prices_sorted_by_start_and_zone() -> None:
    prices = client(lambda _: httpx.Response(200, text=ONE_HOUR)).imbalance_prices(
        START, END, [BiddingZone.SE3, BiddingZone.FI]
    )

    assert [(p.start, p.zone) for p in prices] == [
        (START + i * INTERVAL, zone) for i in range(4) for zone in (BiddingZone.FI, BiddingZone.SE3)
    ]
    published = prices[1]
    assert published.imbalance_price_eur == 21.0
    assert published.source == "entsoe"
    assert published.resolution == INTERVAL
    assert published.end == START + INTERVAL
    assert published.imbalance_price_dkk is None
    assert published.spot_price_eur is None
    assert published.dominating_direction is None
    assert published.satisfied_demand_mw is None
    assert published.afrr_up_vwa_eur is None
    assert published.afrr_down_vwa_eur is None
    assert published.mfrr_up_price_eur is None
    assert published.mfrr_down_price_eur is None
    assert published.raw == {
        "zone": "SE3",
        "start": "2026-01-15T00:00:00Z",
        "resolution": "PT15M",
        "curveType": "A03",
        "namespace": NS,
        "category": "A04",
        "categories": [{"category": "A04", "amount": 21.0}],
        "amount": 21.0,
        "flow_direction": None,
        "financial_prices": [],
    }


def test_request_params_eic_codes_and_custom_base_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=acknowledgement("No matching data found"))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    local = timezone(timedelta(hours=2))
    with EntsoeClient(KEY, http, base_url="https://example.test/api/") as entsoe:
        entsoe.imbalance_prices(
            START.astimezone(local),
            END.astimezone(local),
            [BiddingZone.SE3, BiddingZone.FI, BiddingZone.SE3],
        )
    assert len(seen) == 2
    assert [r.url.host for r in seen] == ["example.test"] * 2
    assert [r.url.path for r in seen] == ["/api"] * 2
    assert [r.url.params["controlArea_Domain"] for r in seen] == [
        "10YFI-1--------U",
        "10Y1001A1001A46L",
    ]
    for request in seen:
        assert request.url.params["securityToken"] == KEY
        assert request.url.params["documentType"] == "A85"
        assert request.url.params["periodStart"] == "202601150000"
        assert request.url.params["periodEnd"] == "202601150100"


def test_defaults_to_all_twelve_known_eic_codes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=acknowledgement("No matching data found"))

    client(handler).imbalance_prices(START, END)
    assert len(BiddingZone) == 12
    assert [r.url.params["controlArea_Domain"] for r in seen] == [
        "10YDK-1--------W",
        "10YDK-2--------M",
        "10YFI-1--------U",
        "10YNO-1--------2",
        "10YNO-2--------T",
        "10YNO-3--------J",
        "10YNO-4--------9",
        "10Y1001A1001A48H",
        "10Y1001A1001A44P",
        "10Y1001A1001A45N",
        "10Y1001A1001A46L",
        "10Y1001A1001A47J",
    ]


def test_chunks_windows_longer_than_seven_days_per_zone() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=acknowledgement("No matching data found"))

    client(handler).imbalance_prices(START, START + timedelta(days=16), [BiddingZone.SE3])
    windows = [(r.url.params["periodStart"], r.url.params["periodEnd"]) for r in seen]
    assert windows == [
        ("202601150000", "202601220000"),
        ("202601220000", "202601290000"),
        ("202601290000", "202601310000"),
    ]
    assert all(r.url.params["controlArea_Domain"] == "10Y1001A1001A46L" for r in seen)


def test_parses_plain_xml_and_zip_members() -> None:
    first = document(
        series(
            period(
                "2026-01-15T00:00Z",
                "2026-01-15T00:30Z",
                point(1, 1.0),
                point(2, 2.0),
            )
        )
    )
    second = document(
        series(
            period(
                "2026-01-15T00:30Z",
                "2026-01-15T01:00Z",
                point(1, 3.0),
                point(2, 4.0),
            )
        )
    )
    plain = client(lambda _: httpx.Response(200, text=first)).imbalance_prices(
        START, START + timedelta(minutes=30), [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in plain] == [1.0, 2.0]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=zipped(first, second),
            headers={"Content-Type": "application/zip"},
        )

    prices = client(handler).imbalance_prices(START, END, [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.parametrize("status", [200, 400])
def test_no_matching_data_acknowledgement_returns_empty(status: int) -> None:
    prices = client(
        lambda _: httpx.Response(status, text=acknowledgement("No matching data found"))
    ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert prices == []


@pytest.mark.parametrize("status", [200, 400])
def test_other_acknowledgements_raise_source_error_with_reason(status: int) -> None:
    long_reason = "token rejected: " + "x" * 300
    with pytest.raises(SourceError, match="entsoe: acknowledgement") as caught:
        client(
            lambda _: httpx.Response(status, text=acknowledgement(long_reason))
        ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert "token rejected" in str(caught.value)
    assert "x" * 200 not in str(caught.value)


def test_acknowledgement_without_reason_raises() -> None:
    body = f'<Acknowledgement_MarketDocument xmlns="{ACK_NS}"/>'
    with pytest.raises(SourceError, match="without a reason"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_a03_repeats_previous_point_for_missing_positions() -> None:
    body = document(
        series(
            period(
                "2026-01-15T00:00Z",
                "2026-01-15T01:00Z",
                point(1, 10.0),
                point(3, 30.0),
            ),
            curve="A03",
        )
    )
    prices = client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, END, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [10.0, 10.0, 30.0, 30.0]
    assert [p.start for p in prices] == [START + i * INTERVAL for i in range(4)]


def test_a01_uses_each_position_and_rejects_gaps() -> None:
    full = document(
        series(
            period(
                "2026-01-15T00:00Z",
                "2026-01-15T01:00Z",
                point(1, 1.0),
                point(2, 2.0),
                point(3, 3.0),
                point(4, 4.0),
            ),
            curve="A01",
        )
    )
    prices = client(lambda _: httpx.Response(200, text=full)).imbalance_prices(
        START, END, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [1.0, 2.0, 3.0, 4.0]

    gapped = document(
        series(
            period(
                "2026-01-15T00:00Z",
                "2026-01-15T01:00Z",
                point(1, 1.0),
                point(3, 3.0),
            ),
            curve="A01",
        )
    )
    with pytest.raises(SourceError, match="missing position 2"):
        client(lambda _: httpx.Response(200, text=gapped)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_a03_rejects_a_missing_first_position() -> None:
    body = document(
        series(
            period(
                "2026-01-15T00:00Z",
                "2026-01-15T01:00Z",
                point(2, 2.0),
            ),
            curve="A03",
        )
    )
    with pytest.raises(SourceError, match="missing position 1"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_expands_hourly_points_and_trims_to_the_window() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            text=document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T01:00Z",
                        point(1, 42.0, "A05"),
                        resolution="PT60M",
                    )
                )
            ),
        )

    start = START + timedelta(minutes=20)
    end = START + timedelta(minutes=50)
    prices = client(handler).imbalance_prices(start, end, [BiddingZone.SE3])
    assert seen[0].url.params["periodStart"] == "202601150000"
    assert seen[0].url.params["periodEnd"] == "202601150100"
    assert [p.start for p in prices] == [
        START + timedelta(minutes=30),
        START + timedelta(minutes=45),
    ]
    assert all(p.resolution == timedelta(hours=1) for p in prices)
    assert all(p.imbalance_price_eur == 42.0 for p in prices)
    assert prices[0].raw == {
        "zone": "SE3",
        "start": "2026-01-15T00:00:00Z",
        "resolution": "PT60M",
        "curveType": "A03",
        "namespace": NS,
        "category": "A05",
        "categories": [{"category": "A05", "amount": 42.0}],
        "amount": 42.0,
        "flow_direction": None,
        "financial_prices": [],
    }


@pytest.mark.parametrize("namespace", [NS, NS_45, NS_30, NS_ALT])
def test_accepts_any_balancing_document_namespace(namespace: str) -> None:
    body = document(
        series(
            period(
                "2026-01-15T00:00Z",
                "2026-01-15T00:15Z",
                point(1, 21.0),
            )
        ),
        namespace=namespace,
    )
    prices = client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, START + INTERVAL, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [21.0]


def test_collapses_identical_duplicates() -> None:
    quarter = period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1, 21.0))
    body = document(series(quarter), series(quarter))
    prices = client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, END, [BiddingZone.SE3]
    )
    assert len(prices) == 1
    assert prices[0].imbalance_price_eur == 21.0


def test_rejects_dual_prices_for_the_same_interval() -> None:
    body = document(
        series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1, 21.0, "A04"))),
        series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1, 22.0, "A05"))),
    )
    with pytest.raises(SourceError, match="dual imbalance prices are not supported"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_rejects_conflicting_duplicate_positions() -> None:
    body = document(
        series(
            period(
                "2026-01-15T00:00Z",
                "2026-01-15T00:15Z",
                point(1, 21.0),
                point(1, 22.0),
            )
        )
    )
    with pytest.raises(SourceError, match="dual imbalance prices are not supported"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("plain text, not xml", "not XML"),
        ('<Unexpected xmlns="urn:example"/>', "unknown root element"),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                    ),
                    currency="SEK",
                )
            ),
            "currency_Unit.name",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                        resolution="PT30M",
                    )
                )
            ),
            "unsupported resolution",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                    ),
                    curve="A02",
                )
            ),
            "curveType A02 is not supported",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                    ),
                    curve="A05",
                )
            ),
            "curveType A05 is not supported",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                    )
                ),
                namespace="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0",
            ),
            "unsupported namespace",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                    ),
                    curve=None,
                )
            ),
            "missing curveType",
        ),
        (
            document(
                series(
                    period(
                        "invalid",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                    )
                )
            ),
            "unparseable timeInterval time",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                    )
                )
            ),
            "must be UTC",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point("x", 21.0),
                    )
                )
            ),
            "unparseable position",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(0, 21.0),
                    )
                )
            ),
            "not 1-based",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point("99999999999999999999", 21.0),
                    )
                )
            ),
            "beyond Period end",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(2, 21.0),
                    )
                )
            ),
            "beyond Period end",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, "abc"),
                    )
                )
            ),
            "unparseable imbalance_Price.amount",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, "nan"),
                    )
                )
            ),
            "not finite",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, "-inf"),
                    )
                )
            ),
            "not finite",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T02:00Z",
                        "2026-01-15T02:15Z",
                        point(1, 21.0),
                    )
                )
            ),
            "outside requested window",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:07Z",
                        "2026-01-15T00:22Z",
                        point(1, 21.0),
                    )
                )
            ),
            "not on a 15-minute boundary",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        point(1, 21.0),
                        interval="<otherInterval><start>2026-01-15T00:00Z</start></otherInterval>",
                    )
                )
            ),
            "no timeInterval",
        ),
        (
            document(
                series(
                    period(
                        "2026-01-15T00:15Z",
                        "2026-01-15T00:00Z",
                        point(1, 21.0),
                    )
                )
            ),
            "Period end is not after its start",
        ),
    ],
)
def test_rejects_malformed_documents(body: str, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_rejects_non_xml_zip_member_and_invalid_zip() -> None:
    with pytest.raises(SourceError, match="not XML"):
        client(lambda _: httpx.Response(200, content=zipped("plain text"))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    with pytest.raises(SourceError, match="not a valid ZIP"):
        client(lambda _: httpx.Response(200, content=b"PK\x03\x04junk")).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    corrupt = bytearray(zipped(ONE_HOUR))
    corrupt[len(corrupt) // 2] ^= 0xFF
    with pytest.raises(SourceError, match=r"ZIP|not XML"):
        client(lambda _: httpx.Response(200, content=bytes(corrupt))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_rejects_oversize_body_and_oversize_zip_members() -> None:
    limit = 64 * 1024 * 1024
    with pytest.raises(SourceError, match="exceeds"):
        client(lambda _: httpx.Response(200, content=b"x" * (limit + 1))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    with pytest.raises(SourceError, match="uncompressed"):
        client(lambda _: httpx.Response(200, content=zipped(b" " * (limit + 1)))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_retries_429_then_parses() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2.5"}),
            httpx.Response(200, text=ONE_HOUR),
        ]
    )
    sleeps: list[float] = []
    prices = client(lambda _: next(responses), sleeps).imbalance_prices(
        START, END, [BiddingZone.SE3]
    )
    assert sleeps == [2.5]
    assert len(prices) == 4


def test_retry_budget_is_bounded_and_reports_source() -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitError) as caught:
        client(handler, sleeps).imbalance_prices(START, END, [BiddingZone.SE3])
    assert caught.value.source == "entsoe"
    assert caught.value.retry_after == 3.0
    assert len(calls) == 4
    assert sleeps == [3.0] * 3


@pytest.mark.parametrize("status", [401, 403, 500])
def test_http_errors_include_status_and_redact_token(status: int) -> None:
    with pytest.raises(SourceError, match=f"entsoe: HTTP {status}") as caught:
        client(
            lambda _: httpx.Response(status, text=f"securityToken {KEY} rejected")
        ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert KEY not in str(caught.value)
    assert "[REDACTED_SECRET]" in str(caught.value)
    assert KEY not in "".join(traceback.format_exception(caught.value))


def test_transport_error_does_not_leak_token_or_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(KEY, request=request)

    with pytest.raises(SourceError, match="request failed") as caught:
        client(handler).imbalance_prices(START, END, [BiddingZone.SE3])
    assert str(caught.value) == "entsoe: request failed"
    assert KEY not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("key", [None, "", "  "])
def test_missing_key_names_env_var(key: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENTSOE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ENTSOE_API_KEY"):
        EntsoeClient(key)


def test_env_key_fallback_and_explicit_key_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENTSOE_API_KEY", "environment-key")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["securityToken"])
        return httpx.Response(200, text=acknowledgement("No matching data found"))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with EntsoeClient(http=http) as entsoe:
        entsoe.imbalance_prices(START, END, [BiddingZone.SE3])
    with EntsoeClient(KEY, http) as entsoe:
        entsoe.imbalance_prices(START, END, [BiddingZone.SE3])
    assert seen == ["environment-key", KEY]


@pytest.mark.parametrize(
    ("start", "end", "zones", "message"),
    [
        (START.replace(tzinfo=None), END, None, "timezone-aware"),
        (START, END.replace(tzinfo=None), None, "timezone-aware"),
        (END, START, None, "end must be after start"),
        (START, START, None, "end must be after start"),
        (START, END, [], "zones must not be empty"),
    ],
)
def test_rejects_bad_arguments_without_request(
    start: datetime,
    end: datetime,
    zones: list[BiddingZone] | None,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("bad arguments must not send an HTTP request")

    c = client(handler)
    with pytest.raises(ValueError, match=message):
        if zones is None:
            c.imbalance_prices(start, end)
        else:
            c.imbalance_prices(start, end, zones)


@pytest.mark.parametrize("zone", ["SE3", "unknown", 1, None])
def test_rejects_non_enum_zones_without_request(zone: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("unknown zones must not send an HTTP request")

    with pytest.raises(ValueError, match="BiddingZone"):
        client(handler).imbalance_prices(START, END, [zone])


@pytest.mark.parametrize("retries", [-1, True, 1.5])
def test_rejects_bad_retry_budget(retries: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        EntsoeClient(KEY, max_retries=retries)


def test_context_manager_closes_only_owned_client_and_exports() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=acknowledgement("No matching data found"))
        )
    )
    with EntsoeClient(KEY, http):
        pass
    assert not http.is_closed
    with EntsoeClient(KEY) as owned:
        assert not owned._http.is_closed
    assert owned._http.is_closed
    owned.close()
    assert SourceEntsoeClient is EntsoeClient


def test_fixture_v45_a01_pt15m_single_price() -> None:
    prices = client(
        lambda _: httpx.Response(200, content=fixture("balancing-v4_5-a01-single-price.xml"))
    ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [21.0, 21.5, 22.0, 22.5]
    assert [p.start for p in prices] == [START + i * INTERVAL for i in range(4)]
    assert prices[0].raw == {
        "zone": "SE3",
        "start": "2026-01-15T00:00:00Z",
        "resolution": "PT15M",
        "curveType": "A01",
        "namespace": NS_45,
        "category": "A04",
        "categories": [{"category": "A04", "amount": 21.0}],
        "amount": 21.0,
        "flow_direction": "A01",
        "financial_prices": [],
    }
    assert prices[2].raw["flow_direction"] == "A02"


def test_fixture_v30_a03_pt60m_repeats_omitted_positions() -> None:
    end = START + timedelta(hours=3)
    prices = client(
        lambda _: httpx.Response(200, content=fixture("balancing-v3_0-a03-pt60m-omitted.xml"))
    ).imbalance_prices(START, end, [BiddingZone.FI])
    assert [p.imbalance_price_eur for p in prices] == [10.5] * 8 + [12.25] * 4
    assert [p.start for p in prices] == [START + i * INTERVAL for i in range(12)]
    assert all(p.resolution == timedelta(hours=1) for p in prices)
    assert prices[0].raw["namespace"] == NS_30
    assert prices[0].raw["categories"] == [{"category": None, "amount": 10.5}]


def test_fixture_v45_equal_dual_categories_collapse() -> None:
    prices = client(
        lambda _: httpx.Response(200, content=fixture("balancing-v4_5-dual-category-equal.xml"))
    ).imbalance_prices(START + timedelta(hours=1), START + timedelta(hours=2), [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [30.0, 30.5, 31.0, 31.5]
    assert prices[0].raw["namespace"] == NS_45
    assert prices[0].raw["category"] == "A04"
    assert prices[0].raw["categories"] == [
        {"category": "A04", "amount": 30.0},
        {"category": "A05", "amount": 30.0},
    ]


def test_fixture_v45_conflicting_dual_categories_raise() -> None:
    with pytest.raises(SourceError, match="dual imbalance prices are not supported") as caught:
        client(
            lambda _: httpx.Response(
                200, content=fixture("balancing-v4_5-dual-category-conflict.xml")
            )
        ).imbalance_prices(START, END, [BiddingZone.DK1])
    assert "A04" in str(caught.value)
    assert "A05" in str(caught.value)


def test_fixture_v45_financial_price_only_points() -> None:
    prices = client(
        lambda _: httpx.Response(200, content=fixture("balancing-v4_5-financial-price-only.xml"))
    ).imbalance_prices(START + timedelta(hours=2), START + timedelta(hours=3), [BiddingZone.NO1])
    assert [p.imbalance_price_eur for p in prices] == [None] * 4
    assert [p.raw["amount"] for p in prices] == [None] * 4
    assert prices[0].raw["categories"] == [{"category": None, "amount": None}]
    assert prices[0].raw["financial_prices"] == [
        {"amount": 51.25, "direction": "A01", "price_descriptor": "A01"},
        {"amount": -2.5, "direction": "A02", "price_descriptor": "A02"},
    ]
    assert prices[2].raw["financial_prices"][1] == {
        "amount": -2.0,
        "direction": None,
        "price_descriptor": None,
    }
    assert prices[3].raw["financial_prices"] == [
        {"amount": 50.5, "direction": None, "price_descriptor": "A03"}
    ]


def test_fixture_acknowledgement_no_matching_data() -> None:
    prices = client(
        lambda _: httpx.Response(200, content=fixture("acknowledgement-no-data.xml"))
    ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert prices == []


def test_fixture_zip_of_two_documents() -> None:
    prices = client(
        lambda _: httpx.Response(
            200,
            content=fixture("balancing-mixed.zip"),
            headers={"Content-Type": "application/zip"},
        )
    ).imbalance_prices(START, START + timedelta(hours=2), [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [
        21.0,
        21.5,
        22.0,
        22.5,
        30.0,
        30.5,
        31.0,
        31.5,
    ]
    assert all(p.raw["namespace"] == NS_45 for p in prices)


@pytest.mark.live
def test_live_one_hour_of_se3_and_fi() -> None:
    if not os.environ.get("ENTSOE_API_KEY"):
        pytest.skip("ENTSOE_API_KEY not set")
    with EntsoeClient() as entsoe:
        prices = entsoe.imbalance_prices(START, END, [BiddingZone.SE3, BiddingZone.FI])
    assert len(prices) == 8
    assert [(p.start, p.zone) for p in prices] == [
        (START + i * INTERVAL, zone) for i in range(4) for zone in (BiddingZone.FI, BiddingZone.SE3)
    ]
    assert all(p.resolution == INTERVAL for p in prices)


@pytest.mark.live
def test_live_one_day_of_dk1() -> None:
    if not os.environ.get("ENTSOE_API_KEY"):
        pytest.skip("ENTSOE_API_KEY not set")
    with EntsoeClient() as entsoe:
        prices = entsoe.imbalance_prices(START, START + timedelta(days=1), [BiddingZone.DK1])
    assert prices
    assert all(p.zone is BiddingZone.DK1 for p in prices)


@pytest.mark.parametrize("unit", ["KWH", "MW", ""])
def test_rejects_a_price_unit_other_than_mwh(unit: str) -> None:
    body = document(
        series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1, 21.0), resolution="PT15M"))
    ).replace(
        "<curveType>",
        f"<price_Measurement_Unit.name>{unit}</price_Measurement_Unit.name><curveType>",
    )
    with pytest.raises(SourceError, match=r"price_Measurement_Unit.name"):
        client(lambda _: httpx.Response(200, content=body.encode())).imbalance_prices(
            START, START + timedelta(minutes=15), [BiddingZone.SE3]
        )


# --- Audit 2026-10-09 (baseline v0.1.0a3) -------------------------------------------

SE3_EIC = "10Y1001A1001A46L"
FI_EIC = "10YFI-1--------U"


def quarter_series(*, resolution: str = "PT15M", head: str = "", amount: float = 21.0) -> str:
    count = 4 if resolution == "PT15M" else 1
    return (
        f"<TimeSeries>{head}<curveType>A01</curveType>"
        + period(
            "2026-01-15T00:00Z",
            "2026-01-15T01:00Z",
            *(point(index, amount) for index in range(1, count + 1)),
            resolution=resolution,
        )
        + "</TimeSeries>"
    )


def headed(head: str, *series_: str, namespace: str = NS_45) -> str:
    return (
        f'<Balancing_MarketDocument xmlns="{namespace}">{head}{"".join(series_)}'
        "</Balancing_MarketDocument>"
    )


@pytest.mark.parametrize(
    ("namespace", "element"),
    [
        (NS_45, "area_Domain.mRID"),
        (NS_30, "controlArea_Domain.mRID"),
        (NS, "controlArea_Domain.mRID"),
    ],
)
def test_rejects_a_document_for_another_area(namespace: str, element: str) -> None:
    body = headed(
        f'<{element} codingScheme="A01">{SE3_EIC}</{element}>',
        quarter_series(),
        namespace=namespace,
    )
    with pytest.raises(SourceError, match="does not match requested"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


@pytest.mark.parametrize(
    ("namespace", "element"),
    [(NS_45, "area_Domain.mRID"), (NS_30, "controlArea_Domain.mRID")],
)
def test_accepts_a_document_for_the_requested_area(namespace: str, element: str) -> None:
    body = headed(
        f'<{element} codingScheme="A01">{FI_EIC}</{element}>', quarter_series(), namespace=namespace
    )
    prices = client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, END, [BiddingZone.FI]
    )
    assert [p.zone for p in prices] == [BiddingZone.FI] * 4


def test_rejects_equal_prices_published_at_conflicting_resolutions() -> None:
    body = headed("", quarter_series(resolution="PT60M"), quarter_series(resolution="PT15M"))
    with pytest.raises(SourceError, match="conflicting resolutions"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


@pytest.mark.parametrize("namespace", [NS_30, NS])
def test_rejects_a_price_unit_other_than_mwh_in_pre_4_5_documents(namespace: str) -> None:
    head = "<price_Measure_Unit.name>KWH</price_Measure_Unit.name>"
    body = headed("", quarter_series(head=head), namespace=namespace)
    with pytest.raises(SourceError, match=r"unsupported price_Measure_Unit\.name 'KWH'"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


def test_accepts_mwh_in_pre_4_5_documents() -> None:
    head = "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
    body = headed("", quarter_series(head=head), namespace=NS_30)
    assert (
        len(
            client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
                START, END, [BiddingZone.FI]
            )
        )
        == 4
    )


def test_rejects_a_document_type_other_than_a85() -> None:
    body = headed("<type>A86</type>", quarter_series())
    with pytest.raises(SourceError, match="document type 'A86'"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


def test_rejects_a_period_that_is_not_a_whole_number_of_resolutions() -> None:
    body = headed(
        "",
        "<TimeSeries><curveType>A01</curveType>"
        + period("2026-01-15T00:00Z", "2026-01-15T00:30Z", point(1), resolution="PT60M")
        + "</TimeSeries>",
    )
    with pytest.raises(SourceError, match="not a whole number of PT60M"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


def test_corrupt_zip_member_is_a_source_error() -> None:
    archive = bytearray(zipped(ONE_HOUR * 20))
    # Corrupt the deflate stream (the local header and name end at byte 41).
    for index in range(60, 100):
        archive[index] ^= 0xFF
    with pytest.raises(SourceError, match="not a valid ZIP archive"):
        client(lambda _: httpx.Response(200, content=bytes(archive))).imbalance_prices(
            START, END, [BiddingZone.FI]
        )
