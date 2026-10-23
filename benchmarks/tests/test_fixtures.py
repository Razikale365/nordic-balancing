import io
import zipfile

import httpx
import pytest

from benchmarks.fixtures import FixtureError, RecordTransport

KEY = b"0f0e0d0c-aaaa-bbbb-cccc-123456789abc"


def zipped(body: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("member.xml", body)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "body",
    [
        b"error: " + KEY,
        zipped(b"<x>" + KEY + b"</x>"),
        b'{"error": "' + "".join(f"\\u{c:04x}" for c in KEY).encode() + b'"}',
        b"error: " + KEY.replace(b"-", b"%252D"),
    ],
)
def test_a_response_echoing_a_secret_is_refused_before_anything_is_written(tmp_path, body):
    live = httpx.MockTransport(lambda request: httpx.Response(401, content=body))
    transport = RecordTransport(tmp_path / "set", transport=live, secrets=[KEY])
    with httpx.Client(transport=transport) as http, pytest.raises(FixtureError, match="echoes"):
        http.get("https://web-api.tp.entsoe.eu/api?documentType=A85")
    assert list((tmp_path / "set").iterdir()) == []


def test_a_response_without_secrets_is_recorded(tmp_path):
    live = httpx.MockTransport(lambda request: httpx.Response(200, content=zipped(b"<x/>")))
    transport = RecordTransport(tmp_path / "set", transport=live, secrets=[KEY])
    with httpx.Client(transport=transport) as http:
        http.get("https://web-api.tp.entsoe.eu/api?documentType=A85")
    assert sorted(p.name for p in (tmp_path / "set").iterdir()) == ["000001.bin", "manifest.jsonl"]
