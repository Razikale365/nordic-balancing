"""Shared bounded retries so sources handle throttling consistently."""

import zlib
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from nordic_balancing.errors import RateLimitError, SourceError

# Throttling: exhausting the retries raises RateLimitError.
_RATE_LIMIT_STATUSES = frozenset({429, 503})
# Transient gateway failures: retried, then reported as an ordinary HTTP error.
_TRANSIENT_STATUSES = frozenset({502, 504})
_TRANSIENT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.DecodingError,
)
_DEFAULT_RETRY_AFTER = 10.0
# A longer requested wait is not slept through: RateLimitError reports it instead.
_MAX_RETRY_AFTER = 300.0
# A body that decodes to more than this is rejected while it is read, before it is
# held in full; the ENTSO-E path also bounds the members of ZIP responses by it.
_LIMIT = 64 * 1024 * 1024
# Only codings that _decoded inflates in bounded steps are asked for: httpx would
# also offer br and zstd when their packages happen to be installed.
ACCEPT_ENCODING = {"Accept-Encoding": "gzip, deflate"}
# Most bytes one decompression step may produce before the size is checked again.
_DECODE_STEP = 64 * 1024


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
    return _request(
        http,
        base_url,
        path,
        params,
        source=source,
        max_retries=max_retries,
        sleep=sleep,
        decode=lambda response: response.json(),
    )


def check_base_url(base_url: str) -> None:
    """Reject a ``base_url`` that is not an absolute http or https URL."""
    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL as exc:
        raise ValueError("base_url must be an absolute http or https URL") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError("base_url must be an absolute http or https URL")


def _request(
    http: httpx.Client,
    base_url: str,
    path: str,
    params: Mapping[str, str] | list[tuple[str, str]],
    *,
    source: str,
    max_retries: int,
    sleep: Callable[[float], None],
    passthrough_statuses: frozenset[int] = frozenset(),
    decode: Callable[[httpx.Response], Any] | None = None,
) -> Any:
    query_params = params if isinstance(params, Mapping) else tuple(params)
    for attempt in range(max_retries + 1):
        last = attempt == max_retries
        try:
            response = _get(http, f"{base_url}{path}", query_params, source=source, path=path)
        except _TRANSIENT_ERRORS:
            # Timeouts, dropped connections and undecodable bodies are retried;
            # callers wrap the last one.
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
        elif decode is None or response.status_code != 200:
            break
        else:
            # The body is decoded inside the loop so an uninterpretable one is
            # retried like a transport decoding failure, within the same budget.
            # A deeply nested body exhausts the decoder's recursion limit; that
            # is still an uninterpretable body, not an internal error.
            try:
                return decode(response)
            except (ValueError, RecursionError) as exc:
                if last:
                    raise SourceError(f"{source}: response for {path} is not JSON") from exc
                sleep(_DEFAULT_RETRY_AFTER)
    # Only 200 carries a complete body: 204 has none, 202 is not yet processed and 206
    # is a partial body.
    if response.status_code != 200 and response.status_code not in passthrough_statuses:
        detail = f": {response.text[:200]}" if response.is_error and response.text else ""
        # An empty path is deliberate (the URL carries a secret): name no slot.
        where = f" for {path}" if path else ""
        raise SourceError(f"{source}: HTTP {response.status_code}{where}{detail}")
    return response


def _get(
    http: httpx.Client,
    url: str,
    params: Mapping[str, str] | tuple[tuple[str, str], ...],
    *,
    source: str,
    path: str,
) -> httpx.Response:
    # Streamed so the size bound stops the read itself: a buffered get would hold
    # an oversized (or endless, or decompression-bomb) body before it is checked.
    with http.stream("GET", url, params=params, headers=ACCEPT_ENCODING) as response:
        return httpx.Response(
            response.status_code,
            # Raw bytes: a header that is not ASCII (a malformed Retry-After, say)
            # must reach the same checks as before rather than fail to re-encode.
            headers=[
                (name, value)
                for name, value in response.headers.raw
                if name.lower() not in (b"content-encoding", b"content-length")
            ],
            content=read_capped(response, source=source, path=path),
            request=response.request,
        )


def read_capped(response: httpx.Response, *, source: str, path: str) -> bytes:
    """Read a streamed body, raising SourceError once it decodes past ``_LIMIT`` bytes."""
    chunks = []
    size = 0
    for chunk in _decoded(response):
        size += len(chunk)
        if size > _LIMIT:
            # An empty path is deliberate (the URL carries a secret): name no slot.
            where = f" for {path}" if path else ""
            raise SourceError(f"{source}: response{where} exceeds {_LIMIT} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _decoded(response: httpx.Response) -> Iterator[bytes]:
    # httpx inflates each network chunk whole before yielding it, so a small
    # compressed chunk can become an arbitrarily large allocation. gzip and deflate,
    # including chained codings, are inflated here in bounded steps instead; any
    # other coding (only sent if not asked for: see ACCEPT_ENCODING) keeps httpx's.
    if response.is_stream_consumed:
        # Already read in full (a transport that buffers, such as a response built
        # from bytes): nothing is left to bound, only to check.
        yield from response.iter_bytes()
        return
    header = response.headers.get("content-encoding", "")
    codings = [c.strip().lower() for c in header.split(",") if c.strip()]
    codings = [c for c in codings if c != "identity"]
    if any(c not in ("gzip", "x-gzip", "deflate") for c in codings):
        yield from response.iter_bytes()
        return
    chunks: Iterator[bytes] = response.iter_raw()
    # Codings are listed in the order applied, so they are undone in reverse.
    for coding in reversed(codings):
        chunks = _inflate(chunks, coding)
    try:
        yield from chunks
    except zlib.error as exc:
        # Retried like httpx's own decoding failures.
        raise httpx.DecodingError(f"invalid {header} body") from exc


def _inflate(chunks: Iterator[bytes], coding: str) -> Iterator[bytes]:
    if coding != "deflate":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        for raw in chunks:
            yield from _drain(decoder, raw)
        yield decoder.flush()
        return
    # "deflate" is zlib-wrapped by the standard and raw in practice, and a raw
    # stream can begin with bytes that read as a zlib header. Like httpx, try zlib
    # first and fall back to raw, replaying what was read, if it fails before any
    # output; once output exists the format is settled.
    decoder = zlib.decompressobj()
    retained: bytearray | None = bytearray()
    for raw in chunks:
        if retained is None:
            yield from _drain(decoder, raw)
            continue
        retained += raw
        try:
            for out in _drain(decoder, raw):
                retained = None
                yield out
        except zlib.error:
            if retained is None:
                raise
            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            data, retained = bytes(retained), None
            yield from _drain(decoder, data)
        if retained is not None and len(retained) > _LIMIT:
            raise zlib.error("no output from the first bytes of a deflate body")
    if retained is not None and not decoder.eof:
        # zlib accepted every byte yet never finished: the body is raw after all.
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        yield from _drain(decoder, bytes(retained))
    yield decoder.flush()


def _drain(decoder: Any, data: bytes) -> Iterator[bytes]:
    while data:
        out = decoder.decompress(data, _DECODE_STEP)
        if out:
            yield out
        data = decoder.unconsumed_tail


def _retry_after(
    response: httpx.Response, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> float | None:
    """Seconds to wait from ``Retry-After``: delay-seconds or an HTTP-date (RFC 9110)."""
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    whole, dot, fraction = header.partition(".")
    if header.isascii() and whole.isdigit() and (not dot or fraction.isdigit()):
        # delay-seconds is 1*DIGIT (the decimal fraction some sources send is
        # kept). float() saturates rather than overflowing, so a very long
        # delay still raises RateLimitError through the _MAX_RETRY_AFTER cap;
        # the bound keeps the value formattable for the error hint.
        return min(float(header), 1e300)
    return _seconds_until(header, now())


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
