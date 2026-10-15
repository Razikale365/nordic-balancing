from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nordic_balancing._http import _retry_after as retry_after
from nordic_balancing._http import get_bytes, get_json
from nordic_balancing.errors import RateLimitError, SourceError
from nordic_balancing.sources.fingrid import FingridClient

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def header(value: str) -> httpx.Response:
    return httpx.Response(503, headers={"Retry-After": value})


def at_now() -> datetime:
    return NOW


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        ("2.5", 2.5),
        ("-3", 0.0),
        ("inf", None),
        ("nan", None),
        ("invalid", None),
        ("", None),
        # IMF-fixdate, the preferred HTTP-date format.
        ("Mon, 12 Oct 2026 12:02:00 GMT", 120.0),
        # The two obsolete HTTP-date formats RFC 9110 still requires recipients to accept.
        ("Monday, 12-Oct-26 12:00:30 GMT", 30.0),
        ("Mon Oct 12 12:00:45 2026", 45.0),
        # A date already past means retry at once.
        ("Mon, 12 Oct 2026 11:00:00 GMT", 0.0),
        ("Mon, 12 Oct 2026 12:10:00 GMT", 600.0),
        ("Mon, 32 Oct 2026 12:00:00 GMT", None),
    ],
)
def test_retry_after_accepts_delay_seconds_and_http_dates(
    value: str, expected: float | None
) -> None:
    assert retry_after(header(value), at_now) == expected


def test_missing_retry_after_is_none() -> None:
    assert retry_after(httpx.Response(503), at_now) is None


def fetch(responses: list[httpx.Response], sleeps: list[float]) -> bytes:
    replies = iter(responses)
    http = httpx.Client(transport=httpx.MockTransport(lambda _: next(replies)))
    body, _ = get_bytes(
        http, "https://example.test", "/", {}, source="test", max_retries=3, sleep=sleeps.append
    )
    return body


def test_http_date_in_the_past_retries_without_waiting() -> None:
    sleeps: list[float] = []
    body = fetch(
        [header("Sun, 06 Nov 1994 08:49:37 GMT"), httpx.Response(200, content=b"ok")], sleeps
    )
    assert body == b"ok"
    assert sleeps == [0.0]


def test_http_date_beyond_the_cap_raises_without_sleeping() -> None:
    sleeps: list[float] = []
    with pytest.raises(RateLimitError):
        fetch([header("Fri, 31 Dec 2100 23:59:59 GMT")], sleeps)
    assert sleeps == []


@pytest.mark.parametrize("status", [100, 204, 301, 302, 307, 308])
def test_get_bytes_rejects_non_2xx_and_204_statuses(status: int) -> None:
    record_body = b'{"value":1,"private_record_marker":"SYNTHETIC_RECORD"}'
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                headers={"Location": "https://elsewhere.example/login"},
                content=record_body,
            )
        )
    )
    with pytest.raises(SourceError, match=rf"^test: HTTP {status} for /api$") as exc_info:
        get_bytes(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    assert "SYNTHETIC_RECORD" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (100, b"{}"),
        (204, b""),
        (301, b'{"data": []}'),
        (302, b'{"data": [], "pagination": {"total": 0, "lastPage": 1}}'),
        (302, b"<html>Found</html>"),
        (307, b"[]"),
        (308, b"{}"),
    ],
)
def test_get_json_rejects_non_2xx_and_204_statuses(status: int, body: bytes) -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                headers={"Location": "https://elsewhere.example/login"},
                content=body,
            )
        )
    )
    with pytest.raises(SourceError, match=rf"^test: HTTP {status} for /api$") as exc_info:
        get_json(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    assert "SYNTHETIC_RECORD" not in str(exc_info.value)


@pytest.mark.parametrize(
    "body",
    [
        b'{"data": [], "pagination": {"total": 0, "lastPage": 1}}',
        b'{"value":1,"private_record_marker":"SYNTHETIC_RECORD","api_key":"test-fingrid-key"}',
        b"<html>Found</html>",
    ],
)
def test_fingrid_client_reports_redirect_status(body: bytes) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://elsewhere.example/steal"},
            content=body,
        )

    client = FingridClient(
        "test-fingrid-key",
        httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval=0,
        sleep=lambda _: None,
        clock=lambda: 0.0,
    )
    start = datetime(2026, 1, 15, tzinfo=UTC)
    with pytest.raises(
        SourceError, match=r"^fingrid: HTTP 302 for /datasets/319/data$"
    ) as exc_info:
        client.series(319, start, start + timedelta(hours=1))
    assert "SYNTHETIC_RECORD" not in str(exc_info.value)
    assert "test-fingrid-key" not in str(exc_info.value)
    assert len(requested_urls) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Mon, 12 Oct 2026 14:00:00 +02:00", 0.0),
        ("Mon, 12 Oct 2026 14:02:00 +02:00", 120.0),
        ("Mon, 12 Oct 2026 10:01:00 -02:00", 60.0),
    ],
)
def test_retry_after_converts_explicit_numeric_offsets(value: str, expected: float) -> None:
    assert retry_after(header(value), at_now) == expected
    assert retry_after(header("Mon, 12 Oct 2026 14:02:00 +0200"), at_now) == 120.0
    assert retry_after(header("Mon, 12 Oct 2026 12:00:30 -0000"), at_now) == 30.0
    assert retry_after(header("Mon, 12 Oct 2026 12:00:30 -00:00"), at_now) == 30.0


@pytest.mark.parametrize(
    "value",
    [
        "Mon, 12 Oct 2026 14:00:00 CEST",
        "Mon, 12 Oct 2026 12:02:00 INVALID_TZ",
        "Mon, 12 Oct 2026 12:02:00",
        "Mon, 12 Oct 2026 12:02:00 +25:00",
        "Mon, 12 Oct 2026 12:02:00 +02:60",
        "Mon, 12 Oct 2026 12:02:00 +0260",
    ],
)
def test_retry_after_rejects_unrecognised_timezone_names(value: str) -> None:
    assert retry_after(header(value), at_now) is None
    sleeps: list[float] = []
    body = fetch([header(value), httpx.Response(200, content=b"ok")], sleeps)
    assert body == b"ok"
    assert sleeps == [10.0]


@pytest.mark.parametrize(
    "offset",
    [b"+0\xb200", b"-0\xb200", b"+00\xb20", b"+0\xb2:00", b"-00:0\xb3"],
)
def test_retry_after_rejects_non_ascii_offset_bytes(offset: bytes) -> None:
    raw = b"Mon, 12 Oct 2026 12:02:00 " + offset
    response = httpx.Response(503, headers=[(b"Retry-After", raw)])
    assert retry_after(response, at_now) is None
    sleeps: list[float] = []
    body = fetch([response, httpx.Response(200, content=b"ok")], sleeps)
    assert body == b"ok"
    assert sleeps == [10.0]


@pytest.mark.parametrize("status", [100, 204, 301, 302, 307, 308, 600, 999])
@pytest.mark.parametrize("fetch_json", [False, True])
def test_http_status_errors_exclude_response_records(status: int, fetch_json: bool) -> None:
    record_body = (
        b'{"value":1,"private_record_marker":"SYNTHETIC_RECORD","token":"SYNTHETIC_SECRET"}'
    )
    fetch_response = get_json if fetch_json else get_bytes
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, content=record_body))
        ) as http,
        pytest.raises(SourceError) as exc_info,
    ):
        fetch_response(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    message = str(exc_info.value)
    assert message == f"test: HTTP {status} for /api"
    assert "SYNTHETIC_RECORD" not in message
    assert "SYNTHETIC_SECRET" not in message
