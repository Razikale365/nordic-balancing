"""Shared bounded retries so sources handle throttling consistently."""

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from nordic_balancing.errors import RateLimitError, SourceError

_RETRY_STATUSES = frozenset({429, 503})
_DEFAULT_RETRY_AFTER = 10.0


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
    query_params = params if isinstance(params, Mapping) else tuple(params)
    for attempt in range(max_retries + 1):
        response = http.get(f"{base_url}{path}", params=query_params)
        if response.status_code not in _RETRY_STATUSES:
            break
        retry_after = _retry_after(response)
        if attempt == max_retries:
            raise RateLimitError(source, retry_after)
        sleep(retry_after if retry_after is not None else _DEFAULT_RETRY_AFTER)
    if response.is_error:
        raise SourceError(
            f"{source}: HTTP {response.status_code} for {path}: {response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(f"{source}: response for {path} is not JSON") from exc


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        return None
