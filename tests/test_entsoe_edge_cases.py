"""ENTSO-E edge cases: limits, namespaces, flow directions, redaction and pool shutdown.

Each test covers a behaviour that no other test pinned (found by a mutation-testing pass).
"""

import io
import traceback
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    EntsoeClient,
    RateLimitError,
    ReserveProduct,
    SourceError,
)
from nordic_balancing.sources import entsoe as entsoe_module

KEY = "test-entsoe-token"
NS = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:0"
START = datetime(2026, 1, 15, tzinfo=UTC)
END = START + timedelta(hours=1)
CAP_START = datetime(2026, 10, 5, 22, tzinfo=UTC)
Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler, sleeps: list[float] | None = None, **kwargs: Any) -> EntsoeClient:
    recorded = sleeps if sleeps is not None else []
    return EntsoeClient(
        KEY,
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=recorded.append,
        **kwargs,
    )


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
) -> str:
    return (
        f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
        f"<resolution>{resolution}</resolution>{''.join(points)}</Period>"
    )


def series(*periods: str, curve: str | None = "A03", currency: str | None = "EUR") -> str:
    unit = f"<currency_Unit.name>{currency}</currency_Unit.name>" if currency is not None else ""
    tag = f"<curveType>{curve}</curveType>" if curve is not None else ""
    return f"<TimeSeries>{unit}{tag}{''.join(periods)}</TimeSeries>"


def document(*series_: str, namespace: str = NS, head: str = "") -> str:
    return (
        f'<Balancing_MarketDocument xmlns="{namespace}"><type>A85</type>{head}{"".join(series_)}'
        "</Balancing_MarketDocument>"
    )


def zipped(*members: str | bytes, name: str = "member0.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, member in enumerate(members):
            filename = name if index == 0 else f"member{index}.xml"
            archive.writestr(filename, member)
    return buffer.getvalue()


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
    return client(lambda _: httpx.Response(200, text=body)).capacity_market(
        ReserveProduct.FCR_N,
        CAP_START,
        CAP_START + timedelta(hours=3),
        zones=(BiddingZone.NO1,),
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
        else document(series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1))))
    )
    if fail:
        with pytest.raises(SourceError):
            client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
                START, END, [BiddingZone.SE3, BiddingZone.FI]
            )
    else:
        prices = client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3, BiddingZone.FI]
        )
        assert len(prices) == 2
    assert cancelled == [True]


# an overlong but well-formed exception class name is dropped, not echoed.
def test_overlong_exception_class_name_is_not_echoed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise type("x" * 70, (Exception,), {})()

    with pytest.raises(SourceError) as caught:
        client(handler).imbalance_prices(START, END, [BiddingZone.SE3])
    assert str(caught.value) == "entsoe: request failed (Exception)"


# an explicit empty key must raise, not silently use the environment.
def test_explicit_empty_key_does_not_fall_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENTSOE_API_KEY", "environment-key")
    with pytest.raises(ValueError, match="api_key"):
        EntsoeClient("")


# max_retries=0 is valid and makes exactly one attempt.
def test_zero_retry_budget_makes_a_single_attempt() -> None:
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitError):
        client(handler, sleeps, max_retries=0).imbalance_prices(START, END, [BiddingZone.SE3])
    assert len(calls) == 1
    assert sleeps == []


# the outer redaction is the last line of defence for tokens echoed back
# inside parsed remote fields (here area_Domain.mRID).
def test_source_error_redacts_token_echoed_in_remote_fields() -> None:
    body = document(
        series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1))),
        head=f"<area_Domain.mRID>{KEY}</area_Domain.mRID>",
    )
    with pytest.raises(SourceError, match="does not match requested") as caught:
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert "[REDACTED_SECRET]" in str(caught.value)


# a body or uncompressed ZIP members totalling exactly _LIMIT pass.
def test_body_and_zip_members_exactly_at_the_limit_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entsoe_module, "_LIMIT", 2048)
    body = document(series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1))))
    padded = (body + " " * (2048 - len(body))).encode()
    assert len(padded) == 2048
    prices = client(lambda _: httpx.Response(200, content=padded)).imbalance_prices(
        START, START + INTERVAL, [BiddingZone.SE3]
    )
    assert [p.imbalance_price_eur for p in prices] == [21.0]

    archive = zipped(padded)
    prices = client(
        lambda _: httpx.Response(200, content=archive, headers={"Content-Type": "application/zip"})
    ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [21.0]


# a member whose extension is uppercase still counts as XML.
def test_zip_member_with_uppercase_xml_extension_parses() -> None:
    body = document(series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1))))
    archive = zipped(body, name="PRICES.XML")
    prices = client(
        lambda _: httpx.Response(200, content=archive, headers={"Content-Type": "application/zip"})
    ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])
    assert [p.imbalance_price_eur for p in prices] == [21.0]


# other 451-6 namespaces are not balancing documents.
def test_rejects_a_non_balancing_451_6_namespace() -> None:
    body = document(
        series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1))),
        namespace="urn:iec62325.351:tc57wg16:451-6:aggregateddocument:4:5",
    )
    with pytest.raises(SourceError, match="unsupported namespace"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
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
    body = document(
        series(period("2026-01-15T00:00Z", "2026-01-15T00:15Z", point(1))),
        namespace=namespace,
    )
    with pytest.raises(SourceError, match="unsupported namespace"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# a zero-length Period is rejected like a negative one.
def test_rejects_a_zero_length_period() -> None:
    body = document(series(period("2026-01-15T00:00Z", "2026-01-15T00:00Z")))
    with pytest.raises(SourceError, match="Period end is not after its start"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
            START, END, [BiddingZone.SE3]
        )


# point-level capacity flowDirection must also be A03.
def test_rejects_point_level_capacity_flow_direction_other_than_a03() -> None:
    body = capacity_document(
        capacity_point(1, head="<flowDirection.direction>A01</flowDirection.direction>")
    )
    with pytest.raises(SourceError, match=r"capacity flowDirection\.direction must be A03"):
        capacity_prices(body)


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
    return document(
        series(period(*window, flow_point("A04", first))),
        series(period(*window, flow_point("A05", second))),
    )


def test_merged_publications_track_flow_direction() -> None:
    agreed = client(
        lambda _: httpx.Response(200, text=merged_document("A01", "A01"))
    ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])
    assert agreed[0].raw["flow_direction"] == "A01"

    adopted = client(
        lambda _: httpx.Response(200, text=merged_document(None, "A02"))
    ).imbalance_prices(START, START + INTERVAL, [BiddingZone.SE3])
    assert adopted[0].raw["flow_direction"] == "A02"

    with pytest.raises(SourceError, match="conflicting flowDirection"):
        client(lambda _: httpx.Response(200, text=merged_document("A01", "A02"))).imbalance_prices(
            START, START + INTERVAL, [BiddingZone.SE3]
        )


# timeInterval times must be UTC, not merely timezone-aware.
def test_rejects_a_time_interval_with_non_utc_offset() -> None:
    body = document(
        series(
            period(
                "2026-01-15T02:00+02:00",
                "2026-01-15T02:15+02:00",
                point(1),
            )
        )
    )
    with pytest.raises(SourceError, match="must be UTC"):
        client(lambda _: httpx.Response(200, text=body)).imbalance_prices(
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
