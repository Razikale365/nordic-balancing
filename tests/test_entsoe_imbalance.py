"""ENTSO-E imbalance prices: XML parsing, A01/A03 curves, acknowledgements, chunking and errors."""

import io
import logging
import os
import struct
import sys
import traceback
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx
import pytest
from fakes import (
    ACK_NS,
    ENTSOE_KEY,
    NS,
    NS_45,
    entsoe_acknowledgement,
    entsoe_client,
    entsoe_document,
    entsoe_period,
    entsoe_point,
    entsoe_series,
    http_client,
    zipped,
)

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    EntsoeClient,
    ESettClient,
    RateLimitError,
    SourceError,
)
from nordic_balancing.sources import EntsoeClient as SourceEntsoeClient
from nordic_balancing.sources import entsoe as entsoe_module
from nordic_balancing.sources.entsoe import _EIC_CODES

NS_ALT = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:2:1"
NS_30 = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:3:0"
START = datetime(2026, 1, 15, tzinfo=UTC)
END = START + timedelta(hours=1)
FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


ONE_HOUR = entsoe_document(
    entsoe_series(
        entsoe_period(
            "2026-01-15T00:00Z",
            "2026-01-15T01:00Z",
            entsoe_point(1, 21.0, "A04"),
            entsoe_point(2, 21.5, "A04"),
            entsoe_point(3, 22.0, "A05"),
            entsoe_point(4, 22.5, "A05"),
        )
    )
)


def quarter_in(category: str | None, amount: float = 21.0, resolution: str = "PT15M") -> str:
    end = "2026-01-15T01:00Z" if resolution == "PT60M" else "2026-01-15T00:15Z"
    return entsoe_series(
        entsoe_period(
            "2026-01-15T00:00Z", end, entsoe_point(1, amount, category), resolution=resolution
        )
    )


def zone_document(request: httpx.Request) -> httpx.Response:
    eic = request.url.params["controlArea_Domain"]
    start = request.url.params["periodStart"]
    value = float(sorted(_EIC_CODES.values()).index(eic)) + int(start[6:8]) / 100
    begin = f"{start[:4]}-{start[4:6]}-{start[6:8]}T00:00Z"
    finish = request.url.params["periodEnd"]
    stop = f"{finish[:4]}-{finish[4:6]}-{finish[6:8]}T00:00Z"
    return httpx.Response(
        200,
        text=entsoe_document(
            entsoe_series(entsoe_period(begin, stop, entsoe_point(1, value), resolution="PT60M"))
        ),
    )


SE3_EIC = "10Y1001A1001A46L"
FI_EIC = "10YFI-1--------U"


def quarter_series(*, resolution: str = "PT15M", head: str = "", amount: float = 21.0) -> str:
    count = 4 if resolution == "PT15M" else 1
    return (
        f"<TimeSeries>{head}<curveType>A01</curveType>"
        + entsoe_period(
            "2026-01-15T00:00Z",
            "2026-01-15T01:00Z",
            *(entsoe_point(index, amount) for index in range(1, count + 1)),
            resolution=resolution,
        )
        + "</TimeSeries>"
    )


def headed(head: str, *series_: str, namespace: str = NS_45) -> str:
    if "<type>" not in head:
        head = "<type>A85</type>" + head
    return (
        f'<Balancing_MarketDocument xmlns="{namespace}">{head}{"".join(series_)}'
        "</Balancing_MarketDocument>"
    )


# merging equal prices keeps an agreeing flow direction, adopts the
# direction from the publication that has one, and rejects a conflict.
def merged_document(first: str | None, second: str | None) -> str:
    def flow_point(category: str, flow: str | None) -> str:
        direction = f"<flowDirection.direction>{flow}</flowDirection.direction>" if flow else ""
        return (
            f"<Point><position>1</position><imbalance_Price.amount>21.0</imbalance_Price.amount>"
            f"<imbalance_Price.category>{category}</imbalance_Price.category>{direction}</Point>"
        )

    window = "2026-01-15T00:00Z", "2026-01-15T00:15Z"
    return entsoe_document(
        entsoe_series(entsoe_period(*window, flow_point("A04", first))),
        entsoe_series(entsoe_period(*window, flow_point("A05", second))),
    )


# --- Live checks against the real API ----------------------------------------------------------


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


@pytest.mark.live
def test_live_matches_esett_prices_but_not_its_resolution_label() -> None:
    if not os.environ.get("ENTSOE_API_KEY"):
        pytest.skip("ENTSOE_API_KEY not set")
    start = datetime(2025, 3, 17, 23, tzinfo=UTC)
    end = start + timedelta(hours=2)
    with EntsoeClient() as entsoe, ESettClient() as esett:
        prices = entsoe.imbalance_prices(start, end, [BiddingZone.SE3])
        reference = esett.imbalance_prices(start, end, [BiddingZone.SE3])
    assert [p.start for p in prices] == [p.start for p in reference]
    assert [p.imbalance_price_eur for p in prices] == [p.imbalance_price_eur for p in reference]
    assert [p.resolution for p in prices] == [timedelta(hours=1)] * 4 + [INTERVAL] * 4
    assert all(p.resolution == INTERVAL for p in reference)


# --- Fixtures from real documents --------------------------------------------------------------


def test_fixture_v45_a01_pt15m_single_price() -> None:
    prices = entsoe_client(
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
    prices = entsoe_client(
        lambda _: httpx.Response(200, content=fixture("balancing-v3_0-a03-pt60m-omitted.xml"))
    ).imbalance_prices(START, end, [BiddingZone.FI])
    assert [p.imbalance_price_eur for p in prices] == [10.5] * 8 + [12.25] * 4
    assert [p.start for p in prices] == [START + i * INTERVAL for i in range(12)]
    assert all(p.resolution == timedelta(hours=1) for p in prices)
    assert prices[0].raw["namespace"] == NS_30
    assert prices[0].raw["categories"] == [{"category": None, "amount": 10.5}]


def test_fixture_v45_equal_dual_categories_collapse() -> None:
    prices = entsoe_client(
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
        entsoe_client(
            lambda _: httpx.Response(
                200, content=fixture("balancing-v4_5-dual-category-conflict.xml")
            )
        ).imbalance_prices(START, END, [BiddingZone.DK1])
    assert "A04" in str(caught.value)
    assert "A05" in str(caught.value)


def test_fixture_v45_financial_price_only_points() -> None:
    prices = entsoe_client(
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
    prices = entsoe_client(
        lambda _: httpx.Response(200, content=fixture("acknowledgement-no-data.xml"))
    ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert prices == []


def test_fixture_zip_of_two_documents() -> None:
    prices = entsoe_client(
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


# --- Acknowledgements --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 400])
def test_no_matching_data_acknowledgement_returns_empty(status: int) -> None:
    prices = entsoe_client(
        lambda _: httpx.Response(status, text=entsoe_acknowledgement("No matching data found"))
    ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert prices == []


@pytest.mark.parametrize("status", [200, 400])
def test_other_acknowledgements_raise_source_error_with_reason(status: int) -> None:
    long_reason = "token rejected: " + "x" * 300
    with pytest.raises(SourceError, match="entsoe: acknowledgement") as caught:
        entsoe_client(
            lambda _: httpx.Response(status, text=entsoe_acknowledgement(long_reason))
        ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert "token rejected" in str(caught.value)
    assert "x" * 200 not in str(caught.value)


def test_acknowledgement_without_reason_raises() -> None:
    body = f'<Acknowledgement_MarketDocument xmlns="{ACK_NS}"/>'
    with pytest.raises(SourceError, match="without a reason"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize("first", [False, True])
def test_rejects_mixed_texts_in_one_acknowledgement_reason(status: int, first: bool) -> None:
    texts = ("No matching data found", "Request rejected: quota exceeded")
    body = entsoe_acknowledgement(*(texts[::-1] if first else texts))
    with pytest.raises(SourceError, match="Request rejected: quota exceeded"):
        entsoe_client(lambda _: httpx.Response(status, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("status", [200, 400])
def test_multiple_no_data_texts_in_one_acknowledgement_reason_return_empty(status: int) -> None:
    body = entsoe_acknowledgement(
        " No matching data found for SE3 ", "No matching data found for FI"
    )
    prices = entsoe_client(lambda _: httpx.Response(status, text=body)).imbalance_prices(
        START, END, [BiddingZone.SE3]
    )
    assert prices == []


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize("empty", ["", " \t\n"])
@pytest.mark.parametrize("first", [False, True])
def test_rejects_empty_text_beside_no_data_in_one_reason(
    status: int, empty: str, first: bool
) -> None:
    texts = ("No matching data found", empty)
    body = entsoe_acknowledgement(*(texts[::-1] if first else texts))
    with pytest.raises(SourceError, match=r"acknowledgement:.*No matching data found"):
        entsoe_client(lambda _: httpx.Response(status, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_redacts_a_percent_encoded_token_in_an_acknowledgement() -> None:
    key = "ab+c/=="
    encoded = quote(key, safe="")
    body = entsoe_acknowledgement(f"rejected securityToken={encoded}")
    http = http_client(lambda _: httpx.Response(200, text=body))
    with pytest.raises(SourceError) as caught:
        EntsoeClient(key, http, max_retries=0).imbalance_prices(START, END, [BiddingZone.SE3])
    assert encoded not in str(caught.value)
    assert key not in str(caught.value)


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize(
    "encode",
    [
        lambda key: quote(key),  # slash left unescaped
        lambda key: quote(key, safe="").lower(),  # lowercase hex escapes
        lambda key: quote(key, safe="").replace("%2B", "%2b"),  # mixed hex case
    ],
)
def test_redacts_a_token_echoed_in_any_percent_encoding(
    status: int, encode: Callable[[str], str]
) -> None:
    key = "audit/+%?=secret-token"
    echoed = encode(key)
    assert echoed != key
    body = entsoe_acknowledgement(f"rejected {echoed}")
    http = http_client(lambda _: httpx.Response(status, text=body))
    with pytest.raises(SourceError) as caught:
        EntsoeClient(key, http, max_retries=0).imbalance_prices(START, END, [BiddingZone.SE3])
    message = "".join(traceback.format_exception(caught.value))
    assert echoed not in message and "secret-token" not in message
    assert "[REDACTED_SECRET]" in message


@pytest.mark.parametrize("status", [200, 400])
@pytest.mark.parametrize(
    "other",
    [
        f"<text>Request processing failed {ENTSOE_KEY}</text>",
        "<text/>",
        "",
    ],
)
@pytest.mark.parametrize("first", [False, True])
def test_rejects_mixed_no_data_acknowledgements(status: int, other: str, first: bool) -> None:
    failure = f"<Reason><code>999</code>{other}</Reason>"
    body = entsoe_acknowledgement("No matching data found")
    body = (
        body.replace("<Reason>", failure + "<Reason>", 1)
        if first
        else body.replace(
            "</Acknowledgement_MarketDocument>", failure + "</Acknowledgement_MarketDocument>"
        )
    )
    with pytest.raises(SourceError, match="acknowledgement") as caught:
        entsoe_client(lambda _: httpx.Response(status, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    assert ENTSOE_KEY not in "".join(traceback.format_exception(caught.value))


# --- Arguments and client lifecycle ------------------------------------------------------------


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
        return httpx.Response(200, text=entsoe_acknowledgement("No matching data found"))

    http = http_client(handler)
    with EntsoeClient(http=http) as entsoe:
        entsoe.imbalance_prices(START, END, [BiddingZone.SE3])
    with EntsoeClient(ENTSOE_KEY, http) as entsoe:
        entsoe.imbalance_prices(START, END, [BiddingZone.SE3])
    assert seen == ["environment-key", ENTSOE_KEY]


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

    c = entsoe_client(handler)
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
        entsoe_client(handler).imbalance_prices(START, END, [zone])


@pytest.mark.parametrize("retries", [-1, True, 1.5])
def test_rejects_bad_retry_budget(retries: Any) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        EntsoeClient(ENTSOE_KEY, max_retries=retries)


@pytest.mark.parametrize("workers", [0, -1, True, 1.5])
def test_rejects_bad_worker_count(workers: Any) -> None:
    with pytest.raises(ValueError, match="max_workers"):
        EntsoeClient(ENTSOE_KEY, max_workers=workers)


def test_context_manager_closes_only_owned_client_and_exports() -> None:
    http = http_client(
        lambda _: httpx.Response(200, text=entsoe_acknowledgement("No matching data found"))
    )
    with EntsoeClient(ENTSOE_KEY, http):
        pass
    assert not http.is_closed
    with EntsoeClient(ENTSOE_KEY) as owned:
        assert not owned._http.is_closed
    assert owned._http.is_closed
    owned.close()
    assert SourceEntsoeClient is EntsoeClient


@pytest.mark.parametrize("from_env", [False, True])
def test_strips_key_before_request_and_error_redaction(
    from_env: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["securityToken"])
        return httpx.Response(401, text=f"Unauthorized securityToken={ENTSOE_KEY}")

    padded = f" \t{ENTSOE_KEY}\r\n"
    monkeypatch.setenv("ENTSOE_API_KEY", padded)
    http = http_client(handler)
    with pytest.raises(SourceError) as caught:
        EntsoeClient(None if from_env else padded, http).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    assert seen == [ENTSOE_KEY]
    assert ENTSOE_KEY not in "".join(traceback.format_exception(caught.value))
    assert "[REDACTED_SECRET]" in str(caught.value)


@pytest.mark.parametrize("key", ["a b", "a\tb", "a\nb", "a\x01b", "a\x7fb", "aéb"])
@pytest.mark.parametrize("from_env", [False, True])
def test_rejects_non_printable_or_internal_whitespace_keys(
    key: str, from_env: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENTSOE_API_KEY", key)
    with pytest.raises(ValueError, match="api_key") as caught:
        EntsoeClient(None if from_env else key)
    assert key not in str(caught.value)


# --- Token redaction and logging ---------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 500])
def test_http_errors_include_status_and_redact_token(status: int) -> None:
    with pytest.raises(SourceError, match=f"entsoe: HTTP {status}") as caught:
        entsoe_client(
            lambda _: httpx.Response(status, text=f"securityToken {ENTSOE_KEY} rejected")
        ).imbalance_prices(START, END, [BiddingZone.SE3])
    assert ENTSOE_KEY not in str(caught.value)
    assert "[REDACTED_SECRET]" in str(caught.value)
    assert ENTSOE_KEY not in "".join(traceback.format_exception(caught.value))


def test_transport_error_does_not_leak_token_or_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(ENTSOE_KEY, request=request)

    with pytest.raises(SourceError, match="request failed") as caught:
        entsoe_client(handler).imbalance_prices(START, END, [BiddingZone.SE3])
    assert str(caught.value) == "entsoe: request failed (ConnectError)"
    assert ENTSOE_KEY not in "".join(traceback.format_exception(caught.value))


def test_non_httpx_transport_error_does_not_leak_token_or_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"custom transport failed for {request.url}")

    with pytest.raises(SourceError, match="request failed") as caught:
        entsoe_client(handler).imbalance_prices(START, END, [BiddingZone.SE3])
    assert str(caught.value) == "entsoe: request failed (RuntimeError)"
    assert ENTSOE_KEY not in "".join(traceback.format_exception(caught.value))


def test_parallel_requests_raise_the_first_failing_job_with_token_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        zone = request.url.params["controlArea_Domain"]
        if zone in ("10YDK-2--------M", "10YNO-1--------2"):
            return httpx.Response(500, text=f"{zone} securityToken {ENTSOE_KEY}")
        return httpx.Response(200, text=entsoe_acknowledgement("No matching data found"))

    with pytest.raises(SourceError, match="10YDK-2--------M") as caught:
        entsoe_client(handler).imbalance_prices(START, END)
    assert ENTSOE_KEY not in str(caught.value)


@pytest.mark.parametrize("status", [200, 400, 401, 500])
def test_redacts_tokens_before_error_text_truncation(status: int) -> None:
    secret = ENTSOE_KEY + "-0123456789abcdef"
    detail = "x" * 185 + secret + " rejected"
    body = entsoe_acknowledgement(detail) if status in (200, 400) else detail
    http = http_client(lambda _: httpx.Response(status, text=body))
    with pytest.raises(SourceError) as caught:
        EntsoeClient(secret, http).imbalance_prices(START, END, [BiddingZone.SE3])
    assert secret[:15] not in "".join(traceback.format_exception(caught.value))
    assert "[REDACTED" in str(caught.value)
    assert " rejected" not in str(caught.value)


@pytest.mark.parametrize("status", [401, 500])
def test_redacts_a_percent_encoded_token_echoed_by_the_server(status: int) -> None:
    # A token with URL-reserved characters is sent percent-encoded; a server
    # echoing it must not leak that form either.
    key = "ab+c/=="
    encoded = quote(key, safe="")
    assert encoded != key
    http = http_client(
        lambda _: httpx.Response(status, text=f"Invalid parameter securityToken={encoded}")
    )
    with pytest.raises(SourceError) as caught:
        EntsoeClient(key, http, max_retries=0).imbalance_prices(START, END, [BiddingZone.SE3])
    message = "".join(traceback.format_exception(caught.value))
    assert key not in message and encoded not in message
    assert "[REDACTED_SECRET]" in message


def test_token_reaches_logs_only_through_httpx_info_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fetch() -> None:
        entsoe_client(lambda _: httpx.Response(200, text=ONE_HOUR)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )

    # The library configures no logging: httpx keeps its default level and handlers, and
    # nothing the library itself logs carries the token.
    httpx_logger = logging.getLogger("httpx")
    level, handlers = httpx_logger.level, list(httpx_logger.handlers)
    with caplog.at_level(logging.DEBUG, logger="nordic_balancing"):
        fetch()
    assert (httpx_logger.level, httpx_logger.handlers) == (level, handlers)
    assert ENTSOE_KEY not in caplog.text
    # The documented risk: httpx logs each request URL, token included, at INFO.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="httpx"):
        fetch()
    assert ENTSOE_KEY in caplog.text


def test_dynamically_named_transport_exception_is_not_echoed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise type(str(request.url), (Exception,), {})()

    with pytest.raises(SourceError) as caught:
        entsoe_client(handler).imbalance_prices(START, END, [BiddingZone.SE3])
    assert str(caught.value) == "entsoe: request failed (Exception)"
    assert ENTSOE_KEY not in "".join(traceback.format_exception(caught.value))


# an overlong but well-formed exception class name is dropped, not echoed.
def test_overlong_exception_class_name_is_not_echoed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise type("x" * 70, (Exception,), {})()

    with pytest.raises(SourceError) as caught:
        entsoe_client(handler).imbalance_prices(START, END, [BiddingZone.SE3])
    assert str(caught.value) == "entsoe: request failed (Exception)"


# an explicit empty key must raise, not silently use the environment.
def test_explicit_empty_key_does_not_fall_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENTSOE_API_KEY", "environment-key")
    with pytest.raises(ValueError, match="api_key"):
        EntsoeClient("")


# the outer redaction is the last line of defence for tokens echoed back
# inside parsed remote fields (here area_Domain.mRID).
def test_source_error_redacts_token_echoed_in_remote_fields() -> None:
    body = entsoe_document(
        entsoe_series(entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1))),
        head=f"<area_Domain.mRID>{ENTSOE_KEY}</area_Domain.mRID>",
    )
    with pytest.raises(SourceError, match="does not match requested") as caught:
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    assert ENTSOE_KEY not in "".join(traceback.format_exception(caught.value))
    assert "[REDACTED_SECRET]" in str(caught.value)


# --- Retries, transport errors and API key handling --------------------------------------------


def test_retries_429_then_parses() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2.5"}),
            httpx.Response(200, text=ONE_HOUR),
        ]
    )
    sleeps: list[float] = []
    prices = entsoe_client(lambda _: next(responses), sleeps).imbalance_prices(
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
        entsoe_client(handler, sleeps).imbalance_prices(START, END, [BiddingZone.SE3])
    assert caught.value.source == "entsoe"
    assert caught.value.retry_after == 3.0
    assert len(calls) == 4
    assert sleeps == [3.0] * 3


# The request URL carries the token, so the HTTP error names no path at all.
def test_http_error_message_has_no_path_slot() -> None:
    with pytest.raises(SourceError) as caught:
        entsoe_client(lambda _: httpx.Response(404, text="oops")).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    assert str(caught.value) == "entsoe: HTTP 404: oops"


# An empty error body leaves no detail to append, so no trailing ": " either.
@pytest.mark.parametrize("status", [404, 500])
def test_http_error_with_empty_body_has_no_trailing_colon(status: int) -> None:
    with pytest.raises(SourceError) as caught:
        entsoe_client(lambda _: httpx.Response(status, content=b"")).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    assert str(caught.value) == f"entsoe: HTTP {status}"


# max_retries=0 is valid and makes exactly one attempt.
def test_zero_retry_budget_makes_a_single_attempt() -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitError):
        entsoe_client(handler, sleeps, max_retries=0).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    assert len(calls) == 1
    assert sleeps == []


# --- Time intervals and periods ----------------------------------------------------------------


def test_a03_repeats_previous_point_for_missing_positions() -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z",
                "2026-01-15T01:00Z",
                entsoe_point(1, 10.0),
                entsoe_point(3, 30.0),
            ),
            curve="A03",
        )
    )
    prices = entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, END, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [10.0, 10.0, 30.0, 30.0]
    assert [p.start for p in prices] == [START + i * INTERVAL for i in range(4)]


@pytest.mark.parametrize(
    ("resolution", "period_end"),
    [("PT15M", "2025-01-01T01:00Z"), ("PT60M", "2025-01-01T02:00Z")],
)
def test_a03_fill_stops_at_chunk_end_without_next_chunk_data(
    resolution: str, period_end: str
) -> None:
    boundary = datetime(2025, 1, 1, tzinfo=UTC)
    seen: list[tuple[str, str]] = []
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2024-12-31T23:00Z", period_end, entsoe_point(1, 10.0), resolution=resolution
            )
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        begin = request.url.params["periodStart"]
        seen.append((begin, request.url.params["periodEnd"]))
        if begin == "202401010000":
            return httpx.Response(200, text=body)
        return httpx.Response(200, text=entsoe_acknowledgement("No matching data found"))

    prices = entsoe_client(handler, max_workers=1).imbalance_prices(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 1, 10, tzinfo=UTC), [BiddingZone.DK1]
    )
    assert seen == [("202401010000", "202501010000"), ("202501010000", "202501100000")]
    assert [p.start for p in prices] == [
        boundary - timedelta(hours=1) + i * INTERVAL for i in range(4)
    ]
    assert [p.imbalance_price_eur for p in prices] == [10.0] * 4
    assert all(
        p.resolution == timedelta(minutes=15 if resolution == "PT15M" else 60) for p in prices
    )


def test_a03_fill_stops_at_chunk_end_with_partial_next_chunk_data() -> None:
    boundary = datetime(2025, 1, 1, tzinfo=UTC)
    first = entsoe_document(
        entsoe_series(
            entsoe_period("2024-12-31T23:00Z", "2025-01-01T01:00Z", entsoe_point(1, 10.0))
        )
    )
    second = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2025-01-01T00:30Z",
                "2025-01-01T01:00Z",
                entsoe_point(1, 20.0),
                entsoe_point(2, 30.0),
            )
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = first if request.url.params["periodStart"] == "202401010000" else second
        return httpx.Response(200, text=body)

    prices = entsoe_client(handler, max_workers=1).imbalance_prices(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 1, 10, tzinfo=UTC), [BiddingZone.DK1]
    )
    assert [p.start for p in prices] == [
        *(boundary - timedelta(hours=1) + i * INTERVAL for i in range(4)),
        boundary + 2 * INTERVAL,
        boundary + 3 * INTERVAL,
    ]
    assert [p.imbalance_price_eur for p in prices] == [10.0] * 4 + [20.0, 30.0]


def test_a03_fill_still_rejects_explicit_point_outside_chunk_window() -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2024-12-31T23:00Z",
                "2025-01-01T02:00Z",
                entsoe_point(1, 10.0),
                entsoe_point(2, 20.0),
                resolution="PT60M",
            )
        )
    )
    with pytest.raises(SourceError, match="point outside requested window"):
        entsoe_client(lambda _: httpx.Response(200, text=body), max_workers=1).imbalance_prices(
            datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 1, 10, tzinfo=UTC), [BiddingZone.DK1]
        )


def test_a03_fill_work_is_bounded_by_window_for_year_long_period() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    element = ElementTree.fromstring(
        entsoe_period("2024-01-01T00:00Z", "2025-01-01T00:00Z", entsoe_point(1, 10.0))
    )
    points = entsoe_module._period_points(element, "A03", NS, start, start + timedelta(hours=1))
    assert [p.start for p in points] == [start + i * INTERVAL for i in range(4)]
    assert [p.amount for p in points] == [10.0] * 4


def test_a01_uses_each_position_and_rejects_gaps() -> None:
    full = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z",
                "2026-01-15T01:00Z",
                entsoe_point(1, 1.0),
                entsoe_point(2, 2.0),
                entsoe_point(3, 3.0),
                entsoe_point(4, 4.0),
            ),
            curve="A01",
        )
    )
    prices = entsoe_client(lambda _: httpx.Response(200, text=full)).imbalance_prices(
        START, END, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [1.0, 2.0, 3.0, 4.0]

    gapped = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z",
                "2026-01-15T01:00Z",
                entsoe_point(1, 1.0),
                entsoe_point(3, 3.0),
            ),
            curve="A01",
        )
    )
    with pytest.raises(SourceError, match="missing position 2"):
        entsoe_client(lambda _: httpx.Response(200, text=gapped)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_a03_rejects_a_missing_first_position() -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z",
                "2026-01-15T01:00Z",
                entsoe_point(2, 2.0),
            ),
            curve="A03",
        )
    )
    with pytest.raises(SourceError, match="missing position 1"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_expands_hourly_points_and_trims_to_the_window() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            text=entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T01:00Z",
                        entsoe_point(1, 42.0, "A05"),
                        resolution="PT60M",
                    )
                )
            ),
        )

    start = START + timedelta(minutes=20)
    end = START + timedelta(minutes=50)
    prices = entsoe_client(handler).imbalance_prices(start, end, [BiddingZone.SE3])
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


def test_rejects_dual_prices_for_the_same_interval() -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1, 21.0, "A04"))
        ),
        entsoe_series(
            entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1, 22.0, "A05"))
        ),
    )
    with pytest.raises(SourceError, match="dual imbalance prices are not supported"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_rejects_conflicting_duplicate_positions() -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z",
                "2026-01-15T00:15Z",
                entsoe_point(1, 21.0),
                entsoe_point(1, 22.0),
            )
        )
    )
    with pytest.raises(SourceError, match="dual imbalance prices are not supported"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# A digit string longer than the interpreter's integer conversion limit fails
# inside int(): the parser must still report it as a SourceError.
def test_rejects_a_position_beyond_the_integer_conversion_limit() -> None:
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        body = entsoe_document(
            entsoe_series(
                entsoe_period(
                    "2026-01-15T00:00Z",
                    "2026-01-15T00:15Z",
                    entsoe_point("9" * 641, 21.0),
                )
            )
        )
        with pytest.raises(SourceError, match="unparseable position"):
            entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
                START, END, [BiddingZone.SE3]
            )
    finally:
        sys.set_int_max_str_digits(limit)


def test_rejects_a_period_that_is_not_a_whole_number_of_resolutions() -> None:
    body = headed(
        "",
        "<TimeSeries><curveType>A01</curveType>"
        + entsoe_period(
            "2026-01-15T00:00Z", "2026-01-15T00:30Z", entsoe_point(1), resolution="PT60M"
        )
        + "</TimeSeries>",
    )
    with pytest.raises(SourceError, match="not a whole number of PT60M"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


@pytest.mark.parametrize("minute", [15, 30, 45])
@pytest.mark.parametrize("curve", ["A01", "A03"])
def test_rejects_off_hour_hourly_points(minute: int, curve: str) -> None:
    begin = START + timedelta(minutes=minute)
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                begin.isoformat(),
                (begin + timedelta(hours=1)).isoformat(),
                entsoe_point(1),
                resolution="PT60M",
            ),
            curve=curve,
        )
    )
    with pytest.raises(SourceError, match="not on the hour"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("curve", ["A01", "A03"])
@pytest.mark.parametrize("changed", [False, True])
def test_rejects_distinct_categories_at_one_period_position(curve: str, changed: bool) -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z",
                "2026-01-15T00:30Z",
                entsoe_point(1, 20.0, "A04"),
                entsoe_point(1, 20.0, "A05"),
                entsoe_point(2, 30.0 if changed else 20.0, "A04"),
            ),
            curve=curve,
        )
    )
    with pytest.raises(SourceError, match="duplicate position"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# a zero-length Period is rejected like a negative one.
def test_rejects_a_zero_length_period() -> None:
    body = entsoe_document(entsoe_series(entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:00Z")))
    with pytest.raises(SourceError, match="Period end is not after its start"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# timeInterval times must be UTC, not merely timezone-aware.
def test_rejects_a_time_interval_with_non_utc_offset() -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T02:00+02:00",
                "2026-01-15T02:15+02:00",
                entsoe_point(1),
            )
        )
    )
    with pytest.raises(SourceError, match="must be UTC"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# Real documents write timeInterval as YYYY-MM-DDTHH:MMZ with no seconds;
# the Z and +00:00 designators are the only UTC spellings allowed.
@pytest.mark.parametrize(
    "stamp",
    [
        "2026-01-15T00:00Z",
        "2026-01-15T00:00:00Z",
        "2026-01-15T00:00:00.000Z",
        "2026-01-15T00:00:00+00:00",
    ],
)
def test_accepts_canonical_utc_time_intervals(stamp: str) -> None:
    assert entsoe_module._time(stamp) == START


# fromisoformat also parses zero-offset spellings that are not the UTC
# designator: -00:00 (RFC 3339 "offset unknown"), +00, +0000, offset seconds,
# a space separator and the basic layout. All read "must be UTC".
@pytest.mark.parametrize(
    "stamp",
    [
        "2026-01-15T00:00:00-00:00",
        "2026-01-15T00:00:00+00",
        "2026-01-15T00:00:00+0000",
        "2026-01-15T00:00:00+00:00:00",
        "2026-01-15 00:00:00+00:00",
        "20260115T000000+00:00",
    ],
)
def test_rejects_non_canonical_utc_time_intervals(stamp: str) -> None:
    with pytest.raises(SourceError, match="must be UTC"):
        entsoe_module._time(stamp)


# --- Size limits, ZIP and XML handling ---------------------------------------------------------


def test_parses_plain_xml_and_zip_members() -> None:
    first = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z",
                "2026-01-15T00:30Z",
                entsoe_point(1, 1.0),
                entsoe_point(2, 2.0),
            )
        )
    )
    second = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:30Z",
                "2026-01-15T01:00Z",
                entsoe_point(1, 3.0),
                entsoe_point(2, 4.0),
            )
        )
    )
    plain = entsoe_client(lambda _: httpx.Response(200, text=first)).imbalance_prices(
        START, START + timedelta(minutes=30), [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in plain] == [1.0, 2.0]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=zipped(first, second),
            headers={"Content-Type": "application/zip"},
        )

    prices = entsoe_client(handler).imbalance_prices(START, END, [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [1.0, 2.0, 3.0, 4.0]


def test_rejects_non_xml_zip_member_and_invalid_zip() -> None:
    with pytest.raises(SourceError, match="not XML"):
        entsoe_client(lambda _: httpx.Response(200, content=zipped("plain text"))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    with pytest.raises(SourceError, match="not a valid ZIP"):
        entsoe_client(lambda _: httpx.Response(200, content=b"PK\x03\x04junk")).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    corrupt = bytearray(zipped(ONE_HOUR))
    corrupt[len(corrupt) // 2] ^= 0xFF
    with pytest.raises(SourceError, match=r"ZIP|not XML"):
        entsoe_client(lambda _: httpx.Response(200, content=bytes(corrupt))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_rejects_oversize_body_and_oversize_zip_members() -> None:
    limit = 64 * 1024 * 1024
    with pytest.raises(SourceError, match="exceeds"):
        entsoe_client(lambda _: httpx.Response(200, content=b"x" * (limit + 1))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    with pytest.raises(SourceError, match="uncompressed"):
        entsoe_client(
            lambda _: httpx.Response(200, content=zipped(b" " * (limit + 1)))
        ).imbalance_prices(START, END, [BiddingZone.SE3])


def test_corrupt_zip_member_is_a_source_error() -> None:
    archive = bytearray(zipped(ONE_HOUR * 20))
    # Corrupt the deflate stream (the local header and name end at byte 41).
    for index in range(60, 100):
        archive[index] ^= 0xFF
    with pytest.raises(SourceError, match="not a valid ZIP archive"):
        entsoe_client(lambda _: httpx.Response(200, content=bytes(archive))).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_wraps_corrupt_compressed_archives(compression: int) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        archive.writestr("member0.xml", ONE_HOUR * 20)
    corrupt = bytearray(buffer.getvalue())
    for index in range(60, 100):
        corrupt[index] ^= 0xFF
    with pytest.raises(SourceError, match="not a valid ZIP archive"):
        entsoe_client(lambda _: httpx.Response(200, content=bytes(corrupt))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_wraps_corrupt_archive_directory_offset() -> None:
    corrupt = bytearray(zipped(ONE_HOUR * 20))
    offset = corrupt.rfind(b"PK\x05\x06")
    corrupt[offset + 16 : offset + 20] = struct.pack("<I", 0x7FFFFFFF)
    with pytest.raises(SourceError, match="not a valid ZIP archive"):
        entsoe_client(lambda _: httpx.Response(200, content=bytes(corrupt))).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("member", [None, "directory/", "readme.txt"])
def test_rejects_archives_without_xml_members(member: str | None) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if member is not None:
            archive.writestr(member, ONE_HOUR)
    with pytest.raises(SourceError, match="no XML member"):
        entsoe_client(lambda _: httpx.Response(200, content=buffer.getvalue())).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("encoding", ["UTF-32", "not-a-real-encoding"])
def test_wraps_xml_encoding_failures(encoding: str) -> None:
    body = f'<?xml version="1.0" encoding="{encoding}"?>'.encode() + ONE_HOUR.encode()
    with pytest.raises(SourceError, match="not XML"):
        entsoe_client(lambda _: httpx.Response(200, content=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# a body or uncompressed ZIP members totalling exactly _LIMIT pass.
def test_body_and_zip_members_exactly_at_the_limit_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe_module, "_LIMIT", 2048)
    body = entsoe_document(
        entsoe_series(entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1)))
    )
    padded = (body + " " * (2048 - len(body))).encode()
    assert len(padded) == 2048
    prices = entsoe_client(lambda _: httpx.Response(200, content=padded)).imbalance_prices(
        START, START + INTERVAL, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [21.0]

    archive = zipped(padded)
    prices = entsoe_client(
        lambda _: httpx.Response(200, content=archive, headers={"Content-Type": "application/zip"})
    ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [21.0]


# a member whose extension is uppercase still counts as XML.
def test_zip_member_with_uppercase_xml_extension_parses() -> None:
    body = entsoe_document(
        entsoe_series(entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1)))
    )
    archive = zipped(body, name="PRICES.XML")
    prices = entsoe_client(
        lambda _: httpx.Response(200, content=archive, headers={"Content-Type": "application/zip"})
    ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [21.0]


# --- Malformed records and responses -----------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "message"),
    [
        # The same category twice is a duplicate record even with an equal price (C3).
        (entsoe_document(quarter_in("A04"), quarter_in("A04")), "duplicate price for category A04"),
        (entsoe_document(quarter_in(None), quarter_in(None)), "duplicate price for category none"),
        # The same document twice in one ZIP repeats every category.
        (
            zipped(entsoe_document(quarter_in("A04")), entsoe_document(quarter_in("A04"))),
            "category A04",
        ),
        # A repeated position inside one Period goes through the same check.
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0, "A05"),
                        entsoe_point(1, 21.0, "A05"),
                    )
                )
            ),
            "duplicate price for category A05",
        ),
        (entsoe_document(quarter_in("A04"), quarter_in("A05", 22.0)), "dual imbalance prices"),
        (entsoe_document(quarter_in("A04"), quarter_in("A04", 22.0)), "dual imbalance prices"),
        (
            entsoe_document(quarter_in("A04", resolution="PT60M"), quarter_in("A05")),
            "conflicting resolutions",
        ),
    ],
)
def test_rejects_duplicate_or_conflicting_publications(body: str | bytes, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        entsoe_client(lambda _: httpx.Response(200, content=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("plain text, not xml", "not XML"),
        ('<Unexpected xmlns="urn:example"/>', "unknown root element"),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                    ),
                    currency="SEK",
                )
            ),
            "currency_Unit.name",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                        resolution="PT30M",
                    )
                )
            ),
            "unsupported resolution",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                    ),
                    curve="A02",
                )
            ),
            "curveType A02 is not supported",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                    ),
                    curve="A05",
                )
            ),
            "curveType A05 is not supported",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                    ),
                    curve="A04",
                )
            ),
            "curveType A04 is not supported",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                    )
                ),
                namespace="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0",
            ),
            "unsupported namespace",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                    ),
                    curve=None,
                )
            ),
            "missing curveType",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "invalid",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                    )
                )
            ),
            "unparseable timeInterval time",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                    )
                )
            ),
            "must be UTC",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point("x", 21.0),
                    )
                )
            ),
            "unparseable position",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(0, 21.0),
                    )
                )
            ),
            "not 1-based",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point("99999999999999999999", 21.0),
                    )
                )
            ),
            "beyond Period end",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(2, 21.0),
                    )
                )
            ),
            "beyond Period end",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, "abc"),
                    )
                )
            ),
            "unparseable imbalance_Price.amount",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, "nan"),
                    )
                )
            ),
            "not finite",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, "-inf"),
                    )
                )
            ),
            "not finite",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T02:00Z",
                        "2026-01-15T02:15Z",
                        entsoe_point(1, 21.0),
                    )
                )
            ),
            "outside requested window",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:07Z",
                        "2026-01-15T00:22Z",
                        entsoe_point(1, 21.0),
                    )
                )
            ),
            "not on a 15-minute boundary",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, 21.0),
                        interval="<otherInterval><start>2026-01-15T00:00Z</start></otherInterval>",
                    )
                )
            ),
            "no timeInterval",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:15Z",
                        "2026-01-15T00:00Z",
                        entsoe_point(1, 21.0),
                    )
                )
            ),
            "Period end is not after its start",
        ),
        # Python literal forms are not xs:positiveInteger/xs:decimal lexical forms.
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point("1_6", 21.0),
                    )
                )
            ),
            "unparseable position",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point("\uff11\uff12", 21.0),
                    )
                )
            ),
            "unparseable position",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, "1_0"),
                    )
                )
            ),
            "unparseable imbalance_Price.amount",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, "1e5"),
                    )
                )
            ),
            "unparseable imbalance_Price.amount",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        entsoe_point(1, "\uff11\uff12\uff13"),
                    )
                )
            ),
            "unparseable imbalance_Price.amount",
        ),
    ],
)
def test_rejects_malformed_documents(body: str, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# Singleton elements (maxOccurs=1) appearing twice must raise instead of
# silently keeping the first of two conflicting values.
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:15Z",
                        "<Point><position>1</position>"
                        "<imbalance_Price.amount>999.0</imbalance_Price.amount>"
                        "<imbalance_Price.amount>21.0</imbalance_Price.amount></Point>",
                    )
                )
            ),
            "more than one imbalance_Price.amount in Point",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z",
                        "2026-01-15T00:30Z",
                        "<Point><position>1</position><position>2</position>"
                        "<imbalance_Price.amount>21.0</imbalance_Price.amount></Point>"
                        + entsoe_point(2, 30.0),
                    )
                )
            ),
            "more than one position in Point",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1))
                )
            ).replace("<type>A85</type>", "<type>A85</type><type>A86</type>"),
            "more than one type in Balancing_MarketDocument",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1))
                ).replace(
                    "<currency_Unit.name>EUR</currency_Unit.name>",
                    "<currency_Unit.name>EUR</currency_Unit.name>"
                    "<currency_Unit.name>SEK</currency_Unit.name>",
                )
            ),
            "more than one currency_Unit.name in TimeSeries",
        ),
        (
            entsoe_document(
                entsoe_series(
                    entsoe_period(
                        "2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1)
                    ).replace(
                        "<resolution>PT15M</resolution>",
                        "<resolution>PT15M</resolution><resolution>PT60M</resolution>",
                    )
                )
            ),
            "more than one resolution in Period",
        ),
    ],
)
def test_rejects_duplicated_singleton_elements(body: str, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("unit", ["KWH", "MW", ""])
def test_rejects_a_price_unit_other_than_mwh(unit: str) -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1, 21.0), resolution="PT15M"
            )
        )
    ).replace(
        "<curveType>",
        f"<price_Measurement_Unit.name>{unit}</price_Measurement_Unit.name><curveType>",
    )
    with pytest.raises(SourceError, match=r"price_Measurement_Unit.name"):
        entsoe_client(lambda _: httpx.Response(200, content=body.encode())).imbalance_prices(
            START, START + timedelta(minutes=15), [BiddingZone.SE3]
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
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


def test_rejects_equal_prices_published_at_conflicting_resolutions() -> None:
    body = headed("", quarter_series(resolution="PT60M"), quarter_series(resolution="PT15M"))
    with pytest.raises(SourceError, match="conflicting resolutions"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


@pytest.mark.parametrize("namespace", [NS_30, NS])
def test_rejects_a_price_unit_other_than_mwh_in_pre_4_5_documents(namespace: str) -> None:
    head = "<price_Measure_Unit.name>KWH</price_Measure_Unit.name>"
    body = headed("", quarter_series(head=head), namespace=namespace)
    with pytest.raises(SourceError, match=r"unsupported price_Measure_Unit\.name 'KWH'"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


def test_rejects_a_document_type_other_than_a85() -> None:
    body = headed("<type>A86</type>", quarter_series())
    with pytest.raises(SourceError, match="document type 'A86'"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.FI]
        )


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("incoming", [21.0, 99.0])
def test_rejects_duplicate_keys_from_disjoint_chunks(
    workers: int, incoming: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = entsoe_client(
        lambda _: httpx.Response(200, text=entsoe_document(quarter_in("A04"))), max_workers=workers
    )
    price = c.imbalance_prices(START, END, [BiddingZone.SE3])[0]

    def fetch(zone: BiddingZone, begin: datetime, end: datetime) -> dict[Any, Any]:
        value = price if begin == START else replace(price, imbalance_price_eur=incoming)
        return {(zone, price.start): value}

    monkeypatch.setattr(c, "_fetch", fetch)
    with pytest.raises(SourceError, match="duplicate price across chunks"):
        c.imbalance_prices(START, START + timedelta(days=367), [BiddingZone.SE3])


@pytest.mark.parametrize("value", ["", " \t\n"])
@pytest.mark.parametrize(
    "element",
    [
        "type",
        "area_Domain.mRID",
        "controlArea_Domain.mRID",
        "imbalance_Price.category",
        "flowDirection.direction",
        "direction",
        "priceDescriptor.type",
    ],
)
def test_rejects_present_empty_code_elements(element: str, value: str) -> None:
    empty = f"<{element}>{value}</{element}>"
    if element == "type":
        body = ONE_HOUR.replace("<type>A85</type>", empty)
    elif element in ("area_Domain.mRID", "controlArea_Domain.mRID"):
        body = ONE_HOUR.replace("<type>A85</type>", "<type>A85</type>" + empty)
    elif element in ("direction", "priceDescriptor.type"):
        component = f"<Financial_Price><amount>1</amount>{empty}</Financial_Price>"
        body = ONE_HOUR.replace("</Point>", component + "</Point>")
    else:
        body = entsoe_document(
            entsoe_series(
                entsoe_period(
                    "2026-01-15T00:00Z",
                    "2026-01-15T00:15Z",
                    entsoe_point(1).replace("</Point>", empty + "</Point>"),
                )
            )
        )
    with pytest.raises(SourceError, match=element):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


def test_rejects_missing_mandatory_document_type() -> None:
    body = ONE_HOUR.replace("<type>A85</type>", "")
    with pytest.raises(SourceError, match="missing type"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# other 451-6 namespaces are not balancing documents.
def test_rejects_a_non_balancing_451_6_namespace() -> None:
    body = entsoe_document(
        entsoe_series(entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1))),
        namespace="urn:iec62325.351:tc57wg16:451-6:aggregateddocument:4:5",
    )
    with pytest.raises(SourceError, match="unsupported namespace"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# the namespace check matches the whole balancingdocument URN, so a URN merely
# containing the substring, or with extra segments or non-ASCII digits, raises.
@pytest.mark.parametrize(
    "namespace",
    [
        "urn:not-cim:451-6:balancingdocuments-are-great:9:9",
        "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:0:extra",
        "x-urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:0",
        "urn:iec62325.351:tc57wg16:451-6:balancingdocument:\uff14:\uff10",
    ],
)
def test_rejects_lookalike_balancing_document_namespaces(namespace: str) -> None:
    body = entsoe_document(
        entsoe_series(entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1))),
        namespace=namespace,
    )
    with pytest.raises(SourceError, match="unsupported namespace"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# --- Parsing and request shape -----------------------------------------------------------------


def test_parses_quarter_hour_prices_sorted_by_start_and_zone() -> None:
    prices = entsoe_client(lambda _: httpx.Response(200, text=ONE_HOUR)).imbalance_prices(
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
        return httpx.Response(200, text=entsoe_acknowledgement("No matching data found"))

    http = http_client(handler)
    local = timezone(timedelta(hours=2))
    with EntsoeClient(ENTSOE_KEY, http, base_url="https://example.test/api/") as entsoe:
        entsoe.imbalance_prices(
            START.astimezone(local),
            END.astimezone(local),
            [BiddingZone.SE3, BiddingZone.FI, BiddingZone.SE3],
        )
    assert len(seen) == 2
    assert [r.url.host for r in seen] == ["example.test"] * 2
    assert [r.url.path for r in seen] == ["/api"] * 2
    assert sorted(r.url.params["controlArea_Domain"] for r in seen) == [
        "10Y1001A1001A46L",
        "10YFI-1--------U",
    ]
    for request in seen:
        assert request.url.params["securityToken"] == ENTSOE_KEY
        assert request.url.params["documentType"] == "A85"
        assert request.url.params["periodStart"] == "202601150000"
        assert request.url.params["periodEnd"] == "202601150100"


def test_defaults_to_all_twelve_known_eic_codes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=entsoe_acknowledgement("No matching data found"))

    entsoe_client(handler).imbalance_prices(START, END)
    assert len(BiddingZone) == 12
    assert sorted(r.url.params["controlArea_Domain"] for r in seen) == sorted(
        [
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
    )


def test_chunks_windows_longer_than_366_days_per_zone() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=entsoe_acknowledgement("No matching data found"))

    entsoe_client(handler).imbalance_prices(START, START + timedelta(days=740), [BiddingZone.SE3])
    windows = sorted((r.url.params["periodStart"], r.url.params["periodEnd"]) for r in seen)
    assert windows == [
        ("202601150000", "202701160000"),
        ("202701160000", "202801170000"),
        ("202801170000", "202801250000"),
    ]
    assert all(r.url.params["controlArea_Domain"] == "10Y1001A1001A46L" for r in seen)


@pytest.mark.parametrize("namespace", [NS, NS_45, NS_30, NS_ALT])
def test_accepts_any_balancing_document_namespace(namespace: str) -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period(
                "2026-01-15T00:00Z",
                "2026-01-15T00:15Z",
                entsoe_point(1, 21.0),
            )
        ),
        namespace=namespace,
    )
    prices = entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, START + INTERVAL, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [21.0]


def test_equal_prices_in_distinct_categories_collapse_keeping_both() -> None:
    body = entsoe_document(quarter_in("A04"), quarter_in("A05"))
    prices = entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, START + INTERVAL, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [21.0]
    assert [c["category"] for c in prices[0].raw["categories"]] == ["A04", "A05"]


# A leading sign, a bare fraction and a trailing dot are legal lexical forms of
# xs:positiveInteger and xs:decimal.
@pytest.mark.parametrize(
    ("position", "amount", "expected"),
    [("+1", "+21.0", 21.0), ("1", ".5", 0.5), ("1", "5.", 5.0), ("1", "-5.25", -5.25)],
)
def test_accepts_xsd_integer_and_decimal_forms(position: str, amount: str, expected: float) -> None:
    body = entsoe_document(
        entsoe_series(
            entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(position, amount))
        )
    )
    prices = entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, START + INTERVAL, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [expected]


def test_parallel_requests_give_the_same_records_as_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe_module, "_CHUNK", timedelta(days=1))
    end = START + timedelta(days=3)
    sequential = entsoe_client(zone_document, max_workers=1).imbalance_prices(START, end)
    parallel = entsoe_client(zone_document).imbalance_prices(START, end)
    assert len(parallel) == 12 * 3 * 96
    assert parallel == sequential


@pytest.mark.parametrize(
    ("namespace", "element"),
    [(NS_45, "area_Domain.mRID"), (NS_30, "controlArea_Domain.mRID")],
)
def test_accepts_a_document_for_the_requested_area(namespace: str, element: str) -> None:
    body = headed(
        f'<{element} codingScheme="A01">{FI_EIC}</{element}>', quarter_series(), namespace=namespace
    )
    prices = entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
        START, END, [BiddingZone.FI]
    )
    assert [p.zone for p in prices] == [BiddingZone.FI] * 4


def test_accepts_mwh_in_pre_4_5_documents() -> None:
    head = "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
    body = headed("", quarter_series(head=head), namespace=NS_30)
    assert (
        len(
            entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
                START, END, [BiddingZone.FI]
            )
        )
        == 4
    )


# the imbalance pool must also cancel pending jobs when one fails.
@pytest.mark.parametrize("fail", [True, False])
def test_imbalance_pool_shuts_down_with_pending_cancellation(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    cancelled: list[bool] = []

    class RecordingPool(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            cancelled.append(cancel_futures)
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(entsoe_module, "ThreadPoolExecutor", RecordingPool)
    body = (
        "not xml"
        if fail
        else entsoe_document(
            entsoe_series(entsoe_period("2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1)))
        )
    )
    if fail:
        with pytest.raises(SourceError, match="document is not XML"):
            entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
                START, END, [BiddingZone.SE3, BiddingZone.FI]
            )
    else:
        prices = entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3, BiddingZone.FI]
        )
        assert len(prices) == 2
    assert cancelled == [True]


def test_merged_publications_track_flow_direction() -> None:
    agreed = entsoe_client(
        lambda _: httpx.Response(200, text=merged_document("A01", "A01"))
    ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])
    assert agreed[0].raw["flow_direction"] == "A01"

    adopted = entsoe_client(
        lambda _: httpx.Response(200, text=merged_document(None, "A02"))
    ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])
    assert adopted[0].raw["flow_direction"] == "A02"

    with pytest.raises(SourceError, match="conflicting flowDirection"):
        entsoe_client(
            lambda _: httpx.Response(200, text=merged_document("A01", "A02"))
        ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])


@pytest.mark.parametrize("categories", [(None, "A04"), ("A04", "A05", None)])
def test_uncategorised_point_repeating_a_categorised_one_is_a_duplicate(
    categories: tuple[str | None, ...],
) -> None:
    body = entsoe_document(
        *[
            entsoe_series(
                entsoe_period(
                    "2026-01-15T00:00Z", "2026-01-15T00:15Z", entsoe_point(1, 21.0, category)
                )
            )
            for category in categories
        ]
    )
    with pytest.raises(SourceError, match="duplicate price for category"):
        entsoe_client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, START + INTERVAL, [BiddingZone.FI]
        )


def test_zip_with_more_members_than_the_cap_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entsoe_module, "_MAX_MEMBERS", 3)
    archive = zipped(*[ONE_HOUR] * 4)
    with pytest.raises(SourceError, match="more than 3 members"):
        entsoe_client(
            lambda _: httpx.Response(
                200, content=archive, headers={"Content-Type": "application/zip"}
            )
        ).imbalance_prices(START, END, [BiddingZone.SE3])


def test_zip_member_cap_is_ten_thousand() -> None:
    assert entsoe_module._MAX_MEMBERS == 10_000
    archive = zipped(*[b""] * 10_001)
    with pytest.raises(SourceError, match="more than 10000 members"):
        entsoe_module._members(archive)


def test_error_status_carrying_a_data_document_is_rejected() -> None:
    with pytest.raises(SourceError, match="HTTP 400 without an acknowledgement"):
        entsoe_client(lambda _: httpx.Response(400, text=ONE_HOUR)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("archive", [False, True])
def test_error_status_with_an_acknowledgement_name_only_in_a_comment_is_rejected(
    archive: bool,
) -> None:
    body = ONE_HOUR.replace("<type>", "<!-- Acknowledgement_MarketDocument --><type>", 1)
    payload = zipped(body) if archive else body.encode()
    with pytest.raises(SourceError, match="HTTP 400"):
        entsoe_client(lambda _: httpx.Response(400, content=payload)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


@pytest.mark.parametrize("prefix", [False, True])
@pytest.mark.parametrize("archive", [False, True])
def test_utf16_acknowledgement_with_default_or_prefixed_root_returns_empty(
    prefix: bool, archive: bool
) -> None:
    body = entsoe_acknowledgement("No matching data found")
    if prefix:
        body = (
            body.replace("xmlns=", "xmlns:ack=")
            .replace("<Acknowledgement_MarketDocument", "<ack:Acknowledgement_MarketDocument")
            .replace("</Acknowledgement_MarketDocument>", "</ack:Acknowledgement_MarketDocument>")
        )
    payload = ('<?xml version="1.0" encoding="utf-16"?>' + body).encode("utf-16")
    if archive:
        payload = zipped(payload)
    prices = entsoe_client(lambda _: httpx.Response(400, content=payload)).imbalance_prices(
        START, END, [BiddingZone.SE3]
    )
    assert prices == []


def test_zip_directory_entries_count_toward_the_member_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe_module, "_MAX_MEMBERS", 3)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(3):
            archive.writestr(f"dir{index}/", b"")
        archive.writestr("data.xml", ONE_HOUR)
    with pytest.raises(SourceError, match="more than 3 members"):
        entsoe_client(lambda _: httpx.Response(200, content=buffer.getvalue())).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
