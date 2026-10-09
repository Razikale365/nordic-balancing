"""Shared bounded retries so sources handle throttling consistently."""

import math
from collections.abc import Callable, Mapping
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
    if response.is_error and response.status_code not in passthrough_statuses:
        raise SourceError(
            f"{source}: HTTP {response.status_code} for {path}: {response.text[:200]}"
        )
    return response


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        seconds = float(header)
    except ValueError:
        return None
    # "inf" and "nan" parse as floats but are not usable delays.
    return max(seconds, 0.0) if math.isfinite(seconds) else None
