"""Offline replay of the PERF frozen manifest contract. No fallback transport."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    import httpx
    import requests

_SECRET_NAMES = frozenset({"securitytoken", "api_key", "apikey", "api-key", "key"})


class ReplayMiss(RuntimeError):
    """The exact, sanitized request is absent from the frozen set."""


def request_key(method: str, url: str) -> str:
    parts = urlsplit(url)
    if parts.username or parts.password or parts.fragment:
        raise ValueError("Credentials and fragments are forbidden in replay URLs")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _SECRET_NAMES
    ]
    # Stable sort by name, retaining repeated-parameter order, as the recorder does.
    query.sort(key=lambda item: item[0])
    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    return f"{method.upper()} {clean_url}"


class FrozenSet:
    """Load and validate bytes before timing; retain request provenance separately."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.metadata = json.loads((directory / "set.json").read_text(encoding="utf-8"))
        self.entries = {}
        self.used = []
        self.misses = []
        for line in (directory / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            method, url = row["key"].split(" ", 1)
            key = request_key(method, url)
            if key != row["key"] or key in self.entries:
                raise ValueError("Noncanonical or duplicate manifest key")
            path = (directory / row["file"]).resolve()
            if not path.is_relative_to(directory.resolve()):
                raise ValueError("Body path escapes fixture set")
            body = path.read_bytes()
            if len(body) != row["bytes"]:
                raise ValueError("Manifest byte count differs from body")
            self.entries[key] = (row, body)
        if not self.entries:
            raise ValueError("Empty fixture manifest")

    def get(self, method: str, url: str):
        key = request_key(method, url)
        if key not in self.entries:
            self.misses.append(key)
            raise ReplayMiss(key)
        self.used.append(key)
        return self.entries[key]

    def provenance(self):
        return {
            key: {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            for key, (_, body) in self.entries.items()
        }

    def httpx(self) -> httpx.Client:
        import httpx

        def respond(request):
            row, body = self.get(request.method, str(request.url))
            return httpx.Response(row["status"], headers=row["headers"], content=body)

        return httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)

    def requests(self) -> requests.Session:
        import requests

        frozen = self

        class Adapter(requests.adapters.BaseAdapter):
            def send(self, request, **kwargs):
                row, body = frozen.get(request.method, request.url)
                response = requests.Response()
                response.status_code = row["status"]
                response.headers.update(row["headers"])
                response._content = body
                response.url = request.url
                response.request = request
                return response

            def close(self):
                pass

        session = requests.Session()
        session.trust_env = False
        session.mount("https://", Adapter())
        session.mount("http://", Adapter())
        return session

    def urlopen(self, request, **kwargs):
        row, body = self.get(request.get_method(), request.full_url)
        response = io.BytesIO(body)
        response.status = row["status"]
        response.headers = row["headers"]
        if row["status"] >= 400:
            raise urllib.error.HTTPError(
                request.full_url, row["status"], "replay", row["headers"], response
            )
        return response
