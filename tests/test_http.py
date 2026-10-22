import contextlib
import gzip
import tracemalloc
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fakes import (
    http_client,
    svk_package_show,
)

from nordic_balancing import BiddingZone, ReserveProduct, _http
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
    http = http_client(lambda _: next(replies))
    response: httpx.Response = _request(
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
    http = http_client(lambda _: httpx.Response(429, headers={"Retry-After": "2.5"}))
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

    http = http_client(handler)
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

    http = http_client(handler)
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

    http = http_client(handler)
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
    http = http_client(lambda _: httpx.Response(400, text=body))
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
    http = http_client(lambda _: httpx.Response(400))
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
    http = http_client(
        lambda _: httpx.Response(
            status,
            headers={"Location": "https://elsewhere.example/login"},
            content=record_body,
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
    http = http_client(
        lambda _: httpx.Response(
            status,
            headers={"Location": "https://elsewhere.example/login"},
            content=body,
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
    # the max_retries budget like a Content-Encoding failure.
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="not json")

    http = http_client(handler)
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
    http = http_client(lambda _: next(replies))
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
    # JSON bodies share the 64 MiB bound the ENTSO-E path applies;
    # the cap is shrunk here so the test needs no large body.
    monkeypatch.setattr(_http, "_LIMIT", 8)
    http = http_client(lambda _: httpx.Response(200, json={"v": "0123456789"}))
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
    http = http_client(lambda _: httpx.Response(200, content=b"[1]"))
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
    http = http_client(lambda _: httpx.Response(status))
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
        http_client(handler),
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
        http_client(lambda _: httpx.Response(status, content=record_body)) as http,
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


class ChunkedBody(httpx.SyncByteStream):
    """A body of ``total`` spaces in 1 KiB chunks that counts what the client pulled."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        chunk = b" " * 1024
        for _ in range(self.total // len(chunk)):
            self.consumed += len(chunk)
            yield chunk
        raise httpx.ReadError("the body was read to its end")


@pytest.mark.parametrize("source", ["esett", "eds", "entsoe", "fingrid", "svk"])
def test_every_client_stops_reading_a_body_at_the_size_cap(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    # The cap applies while the body streams in, so an endless body is never held in full;
    # it is shrunk here so the test needs no large body.
    monkeypatch.setattr(_http, "_LIMIT", 4096)
    body = ChunkedBody(1024 * 1024)
    start = datetime(2026, 1, 15, tzinfo=UTC)
    end = start + timedelta(minutes=15)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(200, json=svk_package_show())
        return httpx.Response(200, stream=body)

    with http_client(handler) as http:
        clients: dict[str, Callable[[], Any]] = {
            "esett": lambda: ESettClient(http, max_retries=0),
            "eds": lambda: EnergiDataServiceClient(http, max_retries=0),
            "entsoe": lambda: EntsoeClient("key", http, max_retries=0, max_workers=1),
            "fingrid": lambda: FingridClient("key", http, max_retries=0, min_interval=0),
            "svk": lambda: SvKClient(http, max_retries=0),
        }
        with clients[source]() as client, pytest.raises(SourceError, match="exceeds 4096 bytes"):
            if source == "fingrid":
                client.series(319, start, end)
            elif source == "svk":
                client.capacity_market(ReserveProduct.MFRR, start, end)
            else:
                client.imbalance_prices(start, end, [BiddingZone.DK1])
    assert 4096 < body.consumed <= 4096 + 1024


class ClosableBody(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [("gzip", gzip.compress), ("deflate", zlib.compress)],
)
def test_compressed_body_is_bounded_while_it_is_decoded(
    monkeypatch: pytest.MonkeyPatch, encoding: str, compress: Callable[[bytes], bytes]
) -> None:
    # A small compressed body must not be inflated whole before the cap is checked.
    monkeypatch.setattr(_http, "_LIMIT", 4096)
    stream = ClosableBody(compress(b" " * (16 * 1024 * 1024)))
    http = http_client(
        lambda _: httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream)
    )
    with http:
        tracemalloc.start()
        try:
            with pytest.raises(SourceError, match="exceeds 4096"):
                _request(
                    http,
                    "https://example.test",
                    "/",
                    {},
                    source="test",
                    max_retries=0,
                    sleep=lambda _: None,
                )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak < 4096 + 1024 * 1024
    assert stream.closed


def test_gzip_streamed_body_under_the_cap_decodes() -> None:
    payload = b'{"ok": true}' * 1000
    stream = ClosableBody(gzip.compress(payload))
    http = http_client(
        lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)
    )
    with http:
        response = _request(
            http,
            "https://example.test",
            "/",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    assert response.content == payload
    assert stream.closed


def fetch_encoded(
    data: bytes, encoding: str, chunk: int = 0, *, stream: ClosableBody | None = None
) -> bytes:
    pieces = [data] if chunk == 0 else [data[i : i + chunk] for i in range(0, len(data), chunk)]
    body = stream or ClosableBody(*pieces)
    http = http_client(
        lambda _: httpx.Response(200, headers={"Content-Encoding": encoding}, stream=body)
    )
    with http:
        response = _request(
            http,
            "https://example.test",
            "/",
            {},
            source="test",
            max_retries=0,
            sleep=lambda _: None,
        )
    assert body.closed
    content: bytes = response.content
    return content


def chain(inner: bytes, encoding: str) -> bytes:
    return gzip.compress(inner) if encoding == "gzip, gzip" else zlib.compress(inner)


@pytest.mark.parametrize("encoding", ["gzip, gzip", "gzip, deflate"])
def test_chained_compression_past_the_cap_is_bounded(
    monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    monkeypatch.setattr(_http, "_LIMIT", 4096)
    packed = chain(gzip.compress(b" " * (16 * 1024 * 1024)), encoding)
    tracemalloc.start()
    try:
        with pytest.raises(SourceError, match="exceeds 4096"):
            fetch_encoded(packed, encoding)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4096 + 1024 * 1024


@pytest.mark.parametrize("encoding", ["gzip, gzip", "gzip, deflate"])
@pytest.mark.parametrize("chunk", [0, 1])
def test_chained_compression_under_the_cap_decodes(encoding: str, chunk: int) -> None:
    payload = b'{"ok": true}' * 100
    assert fetch_encoded(chain(gzip.compress(payload), encoding), encoding, chunk) == payload


@pytest.mark.parametrize("wbits", [9, 12])
@pytest.mark.parametrize("chunk", [0, 1])
def test_zlib_deflate_with_a_small_window_decodes(wbits: int, chunk: int) -> None:
    payload = b'{"normal":true,"text":"Nordic"}' * 500
    assert fetch_encoded(zlib.compress(payload, wbits=wbits), "deflate", chunk) == payload


def test_raw_deflate_that_starts_with_a_zlib_looking_byte_decodes() -> None:
    packed = b"\x78\x02\x00\xfd\xff[]\x01\x00\x00\xff\xff"
    assert zlib.decompress(packed, -zlib.MAX_WBITS) == b"[]"
    assert fetch_encoded(packed, "deflate") == b"[]"


def test_public_client_reads_a_small_window_deflate_body() -> None:
    packed = zlib.compress(b"[]", wbits=9)
    stream = ClosableBody(packed)
    start = datetime(2026, 1, 15, tzinfo=UTC)
    http = http_client(
        lambda _: httpx.Response(200, headers={"Content-Encoding": "deflate"}, stream=stream)
    )
    with http, ESettClient(http, max_retries=0) as client:
        prices = client.imbalance_prices(start, start + timedelta(minutes=15), [BiddingZone.FI])
    assert prices == []


def ambiguous_raw_deflate(cmf: int, flg: int) -> tuple[bytes, bytes]:
    # Raw deflate whose first two bytes also form a valid zlib header.
    payload = b"[]" + b" " * (flg - 2)
    packed = bytes([cmf, flg, 0, flg ^ 255, 255]) + payload + b"\x01\x00\x00\xff\xff"
    assert ((cmf << 8) | flg) % 31 == 0
    assert zlib.decompress(packed, -zlib.MAX_WBITS) == payload
    return packed, payload


@pytest.mark.parametrize(("cmf", "flg"), [(0x78, 0x9C), (0x18, 0x95)])
@pytest.mark.parametrize("chunk", [0, 1, 7])
def test_raw_deflate_with_a_valid_zlib_header_prefix_decodes(
    cmf: int, flg: int, chunk: int
) -> None:
    packed, payload = ambiguous_raw_deflate(cmf, flg)
    assert fetch_encoded(packed, "deflate", chunk) == payload


@pytest.mark.parametrize(("cmf", "flg"), [(0x78, 0x9C), (0x18, 0x95)])
def test_public_client_reads_ambiguous_raw_deflate(cmf: int, flg: int) -> None:
    packed, _ = ambiguous_raw_deflate(cmf, flg)
    stream = ClosableBody(packed)
    calls = 0
    start = datetime(2026, 1, 15, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"Content-Encoding": "deflate"}, stream=stream)

    with http_client(handler) as http, ESettClient(http, max_retries=1) as client:
        prices = client.imbalance_prices(start, start + timedelta(minutes=15), [BiddingZone.FI])
    assert prices == []
    assert calls == 1


@pytest.mark.parametrize("source", ["esett", "eds", "entsoe", "fingrid", "svk"])
def test_every_client_asks_for_gzip_and_deflate_only(source: str) -> None:
    seen: list[str | None] = []
    start = datetime(2026, 1, 15, tzinfo=UTC)
    end = start + timedelta(minutes=15)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        if request.url.path.endswith("/package_show"):
            return httpx.Response(200, json=svk_package_show())
        return httpx.Response(200, json=[])

    supplied = {"Accept-Encoding": "br, zstd, gzip, deflate", "X-Caller": "kept"}
    with httpx.Client(transport=httpx.MockTransport(handler), headers=supplied) as http:
        clients: dict[str, Callable[[], Any]] = {
            "esett": lambda: ESettClient(http, max_retries=0),
            "eds": lambda: EnergiDataServiceClient(http, max_retries=0),
            "entsoe": lambda: EntsoeClient("key", http, max_retries=0, max_workers=1),
            "fingrid": lambda: FingridClient("key", http, max_retries=0, min_interval=0),
            "svk": lambda: SvKClient(http, max_retries=0),
        }
        with clients[source]() as client, contextlib.suppress(SourceError):
            if source == "fingrid":
                client.series(319, start, end)
            elif source == "svk":
                client.capacity_market(ReserveProduct.MFRR, start, end)
            else:
                client.imbalance_prices(start, end, [BiddingZone.DK1])
        assert http.headers["accept-encoding"] == "br, zstd, gzip, deflate"
        assert http.headers["x-caller"] == "kept"
    assert seen
    assert set(seen) == {"gzip, deflate"}


@pytest.mark.parametrize("base_url", ["http://", "https:///path"])
def test_check_base_url_rejects_an_empty_host(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url must be an absolute http or https URL"):
        check_base_url(base_url)


def test_error_detail_is_cut_at_200_characters() -> None:
    http = http_client(lambda _: httpx.Response(400, text="x" * 200 + "y" * 100))
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
    assert str(exc_info.value) == f"test: HTTP 400 for /api: {'x' * 200}"


@pytest.mark.parametrize("source", ["esett", "eds", "fingrid", "svk"])
def test_query_after_close_of_an_owned_client_raises_runtime_error(source: str) -> None:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    end = start + timedelta(hours=1)
    clients: dict[str, Callable[[], Any]] = {
        "esett": lambda: ESettClient(),
        "eds": lambda: EnergiDataServiceClient(),
        "fingrid": lambda: FingridClient("key"),
        "svk": lambda: SvKClient(),
    }
    client = clients[source]()
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        if source == "svk":
            client.capacity_market(ReserveProduct.MFRR, start, end)
        else:
            client.imbalance_prices(start, end)


def test_entsoe_query_after_close_of_an_owned_client_is_a_source_error() -> None:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    client = EntsoeClient("key")
    client.close()
    with pytest.raises(SourceError, match="request failed"):
        client.imbalance_prices(start, start + timedelta(hours=1))
