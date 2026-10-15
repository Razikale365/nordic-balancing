"""Shared bounded retries so sources handle throttling consistently."""

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from nordic_balancing.errors import RateLimitError, SourceError

# Throttling: exhausting the retries raises RateLimitError.
_RATE_LIMIT_STATUSES = frozenset({429, 503})
# Transient gateway failures: retried, then reported as an ordinary HTTP error.
_TRANSIENT_STATUSES = frozenset({502, 504})
_TRANSIENT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
_DEFAULT_RETRY_AFTER = 10.0
# A longer requested wait is not slept through: RateLimitError reports it instead.
_MAX_RETRY_AFTER = 300.0


def get_json(
    http: httpx.Client,
    base_url: str,
    path: str,
    params: Mapping[str, str] | list[tuple[str, str]],
    *,
    source: str,
    max_retries: int,
    sleep: Callable[[float], None],
) -> Any:
    response = _request(
        http,
        base_url,
        path,
        params,
        source=source,
        max_retries=max_retries,
        sleep=sleep,
        passthrough_statuses=frozenset(),
    )
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(f"{source}: response for {path} is not JSON") from exc


def get_bytes(
    http: httpx.Client,
    base_url: str,
    path: str,
    params: Mapping[str, str] | list[tuple[str, str]],
    *,
    source: str,
    max_retries: int,
    sleep: Callable[[float], None],
    passthrough_statuses: frozenset[int] = frozenset(),
) -> tuple[bytes, str]:
    response = _request(
        http,
        base_url,
        path,
        params,
        source=source,
        max_retries=max_retries,
        sleep=sleep,
        passthrough_statuses=passthrough_statuses,
    )
    return response.content, response.headers.get("content-type", "")


def _request(
    http: httpx.Client,
    base_url: str,
    path: str,
    params: Mapping[str, str] | list[tuple[str, str]],
    *,
    source: str,
    max_retries: int,
    sleep: Callable[[float], None],
    passthrough_statuses: frozenset[int],
) -> httpx.Response:
    query_params = params if isinstance(params, Mapping) else tuple(params)
    for attempt in range(max_retries + 1):
        last = attempt == max_retries
        try:
            response = http.get(f"{base_url}{path}", params=query_params)
        except _TRANSIENT_ERRORS:
            # Timeouts and dropped connections are retried; callers wrap the last one.
            if last:
                raise
            sleep(_DEFAULT_RETRY_AFTER)
            continue
        if response.status_code in _RATE_LIMIT_STATUSES:
            retry_after = _retry_after(response)
            if last or (retry_after is not None and retry_after > _MAX_RETRY_AFTER):
                raise RateLimitError(source, retry_after)
            sleep(retry_after if retry_after is not None else _DEFAULT_RETRY_AFTER)
        elif response.status_code in _TRANSIENT_STATUSES and not last:
            sleep(_DEFAULT_RETRY_AFTER)
        else:
            break
    if (
        not response.is_success or response.status_code == 204
    ) and response.status_code not in passthrough_statuses:
        detail = f": {response.text[:200]}" if response.is_error else ""
        raise SourceError(f"{source}: HTTP {response.status_code} for {path}{detail}")
    return response


def _retry_after(
    response: httpx.Response, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> float | None:
    """Seconds to wait from ``Retry-After``: delay-seconds or an HTTP-date (RFC 9110)."""
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        seconds = float(header)
    except ValueError:
        return _seconds_until(header, now())
    # "inf" and "nan" parse as floats but are not usable delays.
    return max(seconds, 0.0) if math.isfinite(seconds) else None


def _seconds_until(header: str, now: datetime) -> float | None:
    if not header.isascii():
        return None
    tokens = header.split()
    if (
        len(tokens) == 5
        and tokens[0].isalpha()
        and tokens[1].isalpha()
        and tokens[2].isdigit()
        and ":" in tokens[3]
        and len(tokens[4]) == 4
        and tokens[4].isdigit()
    ):
        # The obsolete asctime HTTP-date format has an implicit GMT zone.
        header = f"{header} GMT"
    elif tokens:
        tz = tokens[-1]
        if len(tz) == 6 and tz[0] in "+-" and tz[3] == ":":
            tz = tz[:3] + tz[4:]
        if len(tz) == 5 and tz[0] in "+-" and tz[1:].isdigit():
            if int(tz[1:3]) >= 24 or int(tz[3:5]) >= 60:
                return None
            tokens[-1] = "+0000" if tz == "-0000" else tz
            header = " ".join(tokens)
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    # A date already past means retry now.
    return max((when - now).total_seconds(), 0.0)
