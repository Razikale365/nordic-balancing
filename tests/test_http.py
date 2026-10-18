from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from nordic_balancing import BiddingZone, _http
from nordic_balancing._http import _request, check_base_url, get_json
from nordic_balancing._http import _retry_after as retry_after
from nordic_balancing._validate import finite_number
from nordic_balancing.errors import RateLimitError, SourceError
from nordic_balancing.sources.energidataservice import EnergiDataServiceClient
from nordic_balancing.sources.entsoe import EntsoeClient
from nordic_balancing.sources.esett import ESettClient
from nordic_balancing.sources.fingrid import FingridClient
from nordic_balancing.sources.svk import SvKClient

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
        # A signed delay is not delay-seconds: 1*DIGIT carries no sign.
        ("-3", None),
        ("inf", None),
        ("nan", None),
        ("invalid", None),
        ("", None),
        # IMF-fixdate, the preferred HTTP-date format.
        ("Mon, 12 Oct 2026 12:02:00 GMT", 120.0),
        # The two obsolete HTTP-date formats RFC 9110 still requires recipients to accept.
        ("Monday, 12-Oct-26 12:00:30 GMT", 30.0),
        ("Mon Oct 12 12:00:45 2026", 45.0),
        # An asctime-style date followed by an explicit zone offset still parses.
        ("Mon Oct 12 12:00:45 2026 -02:00", 7245.0),
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
    response = _request(
        http, "https://example.test", "/", {}, source="test", max_retries=3, sleep=sleeps.append
    )
    return response.content


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


@pytest.mark.parametrize("value", ["120", "300"])
def test_retry_after_up_to_and_at_the_cap_is_slept(value: str) -> None:
    sleeps: list[float] = []
    body = fetch([header(value), httpx.Response(200, content=b"ok")], sleeps)
    assert body == b"ok"
    assert sleeps == [float(value)]


def test_retry_after_beyond_the_cap_raises() -> None:
    sleeps: list[float] = []
    with pytest.raises(RateLimitError):
        fetch([header("301"), httpx.Response(200, content=b"ok")], sleeps)
    assert sleeps == []


@pytest.mark.parametrize("value", ["5_0", "1_000", "+120", "5.", "1e400", "-3", " 120 ", ".5"])
def test_retry_after_rejects_non_digit_delay_seconds(value: str) -> None:
    assert retry_after(header(value), at_now) is None
    sleeps: list[float] = []
    body = fetch([header(value), httpx.Response(200, content=b"ok")], sleeps)
    assert body == b"ok"
    assert sleeps == [10.0]


def test_retry_after_very_long_delay_seconds_raises() -> None:
    sleeps: list[float] = []
    with pytest.raises(RateLimitError):
        fetch([header("9" * 400), httpx.Response(200, content=b"ok")], sleeps)
    assert sleeps == []


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbc\x91\xef\xbc\x92\xef\xbc\x93",  # full-width digits
        b"\xd9\xa1\xd9\xa2\xd9\xa3",  # Arabic-Indic digits
    ],
)
def test_retry_after_rejects_non_ascii_delay_seconds(raw: bytes) -> None:
    # str.isdigit() accepts non-ASCII decimal digits, but delay-seconds is 1*DIGIT
    # (ASCII only), so these are malformed: None and the default delay.
    response = httpx.Response(503, headers=[(b"Retry-After", raw)])
    assert retry_after(response, at_now) is None
    sleeps: list[float] = []
    body = fetch([response, httpx.Response(200, content=b"ok")], sleeps)
    assert body == b"ok"
    assert sleeps == [10.0]


def test_exhausted_retries_report_the_fractional_retry_after() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "2.5"}))
    )
    with pytest.raises(RateLimitError) as exc_info:
        _request(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    assert exc_info.value.retry_after == 2.5
    assert exc_info.value.source == "test"
    assert str(exc_info.value) == "test rate limit exceeded; retry after 2.5 s"


def test_transient_transport_error_is_retried() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        return httpx.Response(200, content=b"ok")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    body = _request(
        http, "https://example.test", "/", {}, source="test", max_retries=3, sleep=sleeps.append
    ).content
    assert body == b"ok"
    assert len(calls) == 2
    assert sleeps == [10.0]


def corrupt_gzip(status: int) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Encoding": "gzip"},
        content=b"\x1f\x8b\x08\x00truncated-gzip",
    )


def test_undecodable_body_is_retried_then_propagates() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return corrupt_gzip(200)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    with pytest.raises(httpx.DecodingError):
        _request(
            http,
            "https://example.test",
            "/",
            {},
            source="test",
            max_retries=3,
            sleep=sleeps.append,
        )
    assert len(calls) == 4
    assert sleeps == [10.0] * 3


def test_undecodable_rate_limit_body_is_still_retried() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return corrupt_gzip(429)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    with pytest.raises(httpx.DecodingError):
        _request(
            http,
            "https://example.test",
            "/",
            {},
            source="test",
            max_retries=3,
            sleep=sleeps.append,
        )
    assert len(calls) == 4
    assert sleeps == [10.0] * 3


def test_error_detail_includes_the_response_body() -> None:
    body = "upstream failure detail: request a1b2c3 rejected"
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, text=body)))
    with pytest.raises(SourceError) as exc_info:
        _request(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    assert str(exc_info.value) == f"test: HTTP 400 for /api: {body}"


def test_error_detail_is_omitted_for_an_empty_body() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400)))
    with pytest.raises(SourceError) as exc_info:
        _request(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    assert str(exc_info.value) == "test: HTTP 400 for /api"


def test_finite_number_coerces_ints_to_float() -> None:
    number = finite_number(21, source="test", column="value")
    assert type(number) is float
    assert number == 21.0


@pytest.mark.parametrize("status", [100, 201, 202, 203, 204, 206, 301, 302, 307, 308])
def test_request_rejects_every_status_but_200(status: int) -> None:
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
        _request(
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


def test_get_json_retries_an_undecodable_body_then_raises() -> None:
    # A 200 body that is not JSON is an undecodable body: it is retried within
    # the max_retries budget like a Content-Encoding failure (B11, OPUS-6/DS4-2).
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="not json")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    with pytest.raises(SourceError, match=r"^test: response for /api is not JSON$"):
        get_json(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=3,
            sleep=sleeps.append,
        )
    assert len(calls) == 4
    assert sleeps == [10.0] * 3


def test_get_json_succeeds_when_a_retry_decodes() -> None:
    replies = iter([httpx.Response(200, text="not json"), httpx.Response(200, json={"ok": True})])
    http = httpx.Client(transport=httpx.MockTransport(lambda _: next(replies)))
    assert get_json(
        http,
        "https://example.test",
        "/api",
        {},
        source="test",
        max_retries=3,
        sleep=lambda _: None,
    ) == {"ok": True}


def test_get_json_rejects_a_body_over_the_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # JSON bodies share the 64 MiB bound the ENTSO-E path applies (B11, DS5-2);
    # the cap is shrunk here so the test needs no large body.
    monkeypatch.setattr(_http, "_LIMIT", 8)
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"v": "0123456789"}))
    )
    with pytest.raises(SourceError, match=r"^test: response for /api exceeds 8 bytes$"):
        get_json(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )


def test_get_json_reports_a_deeply_nested_body_as_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A deeply nested body can exhaust the decoder's recursion limit; that
    # RecursionError is the same contract breach as malformed JSON. How deep a body
    # must be depends on the Python version (3.14 decodes 50,000 levels), so the
    # decoder's failure is simulated rather than reached by depth.
    def exhausted(self: httpx.Response, **kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")

    monkeypatch.setattr(httpx.Response, "json", exhausted)
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"[1]"))
    )
    with pytest.raises(SourceError) as exc_info:
        get_json(
            http,
            "https://example.test",
            "/api",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    assert str(exc_info.value) == "test: response for /api is not JSON"


@pytest.mark.parametrize("status", [302, 204])
def test_entsoe_status_outside_passthrough_has_no_path_slot(status: int) -> None:
    # ENTSO-E calls _request with an empty path because the URL holds the token,
    # so statuses raised by the shared layer (redirects, 2xx != 200) name no path.
    http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status)))
    client = EntsoeClient("test-entsoe-token", http, max_retries=0, sleep=lambda _: None)
    start = datetime(2026, 1, 15, tzinfo=UTC)
    with pytest.raises(SourceError) as exc_info:
        client.imbalance_prices(start, start + timedelta(hours=1), zones=[BiddingZone.NO1])
    assert str(exc_info.value) == f"entsoe: HTTP {status}"


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


@pytest.mark.parametrize("status", [100, 202, 204, 206, 301, 302, 307, 308, 600, 999])
@pytest.mark.parametrize("fetch_json", [False, True])
def test_http_status_errors_exclude_response_records(status: int, fetch_json: bool) -> None:
    record_body = (
        b'{"value":1,"private_record_marker":"SYNTHETIC_RECORD","token":"SYNTHETIC_SECRET"}'
    )
    fetch_response = get_json if fetch_json else _request
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


@pytest.mark.parametrize(
    "base_url",
    ["http://[::1", "http://x/\x00", "not-a-url", "ftp://x"],
)
def test_check_base_url_rejects_urls_without_http_host(base_url: str) -> None:
    with pytest.raises(ValueError, match=r"^base_url must be an absolute http or https URL$"):
        check_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.energidataservice.dk",
        "http://example.test:8080/base",
        "https://example.test",
    ],
)
def test_check_base_url_accepts_absolute_http_urls(base_url: str) -> None:
    check_base_url(base_url)


@pytest.mark.parametrize(
    "make",
    [
        lambda url: EnergiDataServiceClient(base_url=url),
        lambda url: ESettClient(base_url=url),
        lambda url: FingridClient(api_key="key", base_url=url),
        lambda url: EntsoeClient(api_key="key", base_url=url),
        lambda url: SvKClient(base_url=url),
    ],
    ids=["eds", "esett", "fingrid", "entsoe", "svk"],
)
@pytest.mark.parametrize("base_url", ["http://[::1", "not-a-url", "ftp://example.com"])
def test_every_client_rejects_a_malformed_base_url(
    make: Callable[[str], object], base_url: str
) -> None:
    with pytest.raises(ValueError, match="base_url must be an absolute http or https URL"):
        make(base_url)
