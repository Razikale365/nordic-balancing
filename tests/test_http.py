from datetime import UTC, datetime

import httpx
import pytest

from nordic_balancing._http import _retry_after as retry_after
from nordic_balancing._http import get_bytes
from nordic_balancing.errors import RateLimitError

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
