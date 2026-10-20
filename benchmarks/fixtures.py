"""Record and replay ``httpx`` transports for the recorded fixture sets.

Record: pass ``httpx.Client(transport=RecordTransport(set_dir))`` as a client's
``http=`` argument. Every response body is stored verbatim in ``NNNNNN.bin``
and one ``manifest.jsonl`` line is appended per exchange. Secrets are never
written: API-token query parameters are removed from the manifest key and no
request headers or bodies are recorded at all.

Replay: ``ReplayTransport(set_dir)`` answers each request with the recorded
response for its canonical key; an unrecorded key raises ``ReplayMissError``
rather than returning a synthetic response.

Recorded sets live under ``FIXTURES_ROOT``: ``$NB_FIXTURES`` when set, else the
git-ignored ``benchmarks/.fixtures/``. They are API payloads and are never committed.
"""

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

FIXTURES_ROOT = Path(os.environ.get("NB_FIXTURES", Path(__file__).resolve().parent / ".fixtures"))

# Lower-cased query parameter names that can carry credentials. Request headers
# (Fingrid's x-api-key, Authorization) are never persisted, so they need no list.
_SECRET_PARAMS = frozenset({"securitytoken", "api_key", "apikey", "token", "key"})


class FixtureError(RuntimeError):
    """Fixture recording or replay failed."""


class ReplayMissError(FixtureError):
    """Replay was asked for a request the recording never saw."""


def request_key(request: httpx.Request) -> str:
    """Canonical key: METHOD scheme://host/path?sorted query, no secrets."""
    url = request.url
    items = sorted(
        (
            (name, value)
            for name, value in url.params.multi_items()
            if name.lower() not in _SECRET_PARAMS
        ),
        key=lambda item: item[0],
    )
    return f"{request.method} {url.scheme}://{url.host}{url.path}?{urlencode(items)}"


class RecordTransport(httpx.BaseTransport):
    """Wrap a live transport and persist every response in the manifest format above.

    Requests are serialised behind one lock (even a thread-pooled client cannot
    produce concurrent live calls) and paced at least ``min_interval_s`` apart.
    A repeated key keeps only the first response, per the contract; the keys of
    repeats are collected in ``duplicates`` for ``set.json`` notes.
    """

    def __init__(
        self,
        set_dir: str | Path,
        *,
        min_interval_s: float = 0.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.set_dir = Path(set_dir)
        self.set_dir.mkdir(parents=True, exist_ok=True)
        if any(self.set_dir.iterdir()):
            raise FixtureError(f"{self.set_dir} is not empty; refusing to mix recordings")
        self._manifest_path = self.set_dir / "manifest.jsonl"
        self._min_interval_s = min_interval_s
        self._transport = transport if transport is not None else httpx.HTTPTransport()
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._index = 0
        self._seen: set[str] = set()
        self.duplicates: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            wait = self._min_interval_s - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            began = time.perf_counter()
            response = self._transport.handle_request(request)
            body = response.read()
            elapsed = time.perf_counter() - began
            key = request_key(request)
            if key in self._seen:
                self.duplicates.append(key)
                return response
            self._seen.add(key)
            self._index += 1
            name = f"{self._index:06d}.bin"
            (self.set_dir / name).write_bytes(body)
            entry = {
                "key": key,
                "file": name,
                "status": response.status_code,
                "headers": {"content-type": response.headers.get("content-type", "")},
                "bytes": len(body),
                "elapsed_s": elapsed,
            }
            with self._manifest_path.open("a", encoding="utf-8") as manifest:
                manifest.write(json.dumps(entry) + "\n")
        return response

    def close(self) -> None:
        self._transport.close()


class ReplayTransport(httpx.BaseTransport):
    """Serve responses recorded by ``RecordTransport``; misses raise."""

    def __init__(self, set_dir: str | Path) -> None:
        self.set_dir = Path(set_dir)
        self._entries: dict[str, dict] = {}
        manifest_path = self.set_dir / "manifest.jsonl"
        if not manifest_path.is_file():
            raise FixtureError(f"{manifest_path} does not exist")
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                self._entries[entry["key"]] = entry
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        self.requests.append(key)
        entry = self._entries.get(key)
        if entry is None:
            raise ReplayMissError(f"no recorded response for {key}")
        return httpx.Response(
            entry["status"],
            headers={"content-type": entry["headers"]["content-type"]},
            content=(self.set_dir / entry["file"]).read_bytes(),
        )


def replay_client(set_dir: str | Path, **kwargs) -> httpx.Client:
    """An ``httpx.Client`` replaying ``set_dir``; pass extra kwargs (e.g. timeout)."""
    return httpx.Client(transport=ReplayTransport(set_dir), **kwargs)


def load_set(set_dir: str | Path) -> dict:
    return json.loads((Path(set_dir) / "set.json").read_text(encoding="utf-8"))


def load_manifest(set_dir: str | Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (Path(set_dir) / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
