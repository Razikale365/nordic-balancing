import io
import json
import socket
import zipfile
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest

from benchmarks.compare import audit, differences, parameters, summarize, worker
from benchmarks.replay import FrozenSet, ReplayMiss, request_key
from nordic_balancing import BiddingZone

AREA = "10Y1001A1001A46L"
START = datetime(2025, 10, 25, 22, tzinfo=UTC)
END = datetime(2025, 10, 26, 23, tzinfo=UTC)


def make_set(tmp_path, body, start=START, end=END, provider="entsoe", status=200):
    tmp_path.mkdir(parents=True, exist_ok=True)
    if provider == "entsoe":
        params = {
            "documentType": "A85",
            "controlArea_Domain": AREA,
            "periodStart": start.strftime("%Y%m%d%H%M"),
            "periodEnd": end.strftime("%Y%m%d%H%M"),
        }
        url = "https://web-api.tp.entsoe.eu/api?" + urlencode(params)
    else:
        params = {
            "start": start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "end": end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "mba": AREA,
        }
        url = "https://api.opendata.esett.com/EXP14/Prices?" + urlencode(params)
    row = {
        "key": request_key("GET", url),
        "file": "000001.bin",
        "status": status,
        "headers": {
            "content-type": "application/zip" if provider == "entsoe" else "application/json"
        },
        "bytes": len(body),
        "elapsed_s": 0.0,
    }
    (tmp_path / "000001.bin").write_bytes(body)
    (tmp_path / "manifest.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    metadata = {
        "set": "handmade-day",
        "provider": provider,
        "call": "handmade test only",
        "recorded_at": "2026-10-17T00:00:00Z",
        "library_commit": "handmade",
        "requests": 1,
        "total_bytes": len(body),
        "total_elapsed_s": 0,
        "wall_s": 0,
        "records_returned": None,
        "notes": ["handmade; not real market evidence"],
    }
    (tmp_path / "set.json").write_text(json.dumps(metadata), encoding="utf-8")
    return url


def period(
    start, end, count, resolution="PT15M", positions=None, category=True, missing_price=False
):
    points = "".join(
        f"<Point><position>{i}</position>"
        + ("" if missing_price else "<imbalance_Price.amount>21.5</imbalance_Price.amount>")
        + ("<imbalance_Price.category>A04</imbalance_Price.category>" if category else "")
        + "</Point>"
        for i in (positions if positions is not None else range(1, count + 1))
    )
    return (
        f"<Period><timeInterval><start>{start.isoformat()}</start><end>{end.isoformat()}</end>"
        f"</timeInterval><resolution>{resolution}</resolution>{points}</Period>"
    )


def document(*periods):
    xml = (
        '<Balancing_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:5">'
        "<type>A85</type><TimeSeries><currency_Unit.name>EUR</currency_Unit.name>"
        "<curveType>A01</curveType>" + "".join(periods) + "</TimeSeries></Balancing_MarketDocument>"
    )
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("document.xml", xml)
    return stream.getvalue()


def test_both_transports_exact_bytes_and_fail_closed(tmp_path, monkeypatch):
    url = make_set(tmp_path, b"exact\x00bytes", status=418)
    frozen = FrozenSet(tmp_path)

    def forbid(*args, **kwargs):
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    with frozen.httpx() as client:
        response = client.get(url + "&securityToken=not-a-real-token")
        assert response.status_code == 418 and response.content == b"exact\x00bytes"
        with pytest.raises(ReplayMiss):
            client.get(url + "&extra=1")
    with frozen.requests() as session:
        response = session.get(url)
        assert response.status_code == 418 and response.content == b"exact\x00bytes"
        with pytest.raises(ReplayMiss):
            session.get("https://unrecorded.invalid/path")
    assert len(frozen.misses) == 2
    assert "not-a-real-token" not in repr(frozen.used)


def test_canonical_key_preserves_meaning():
    assert request_key("get", "https://a.test/api?z=3&securityToken=x&a=1&a=2") == (
        "GET https://a.test/api?a=1&a=2&z=3"
    )
    assert request_key("GET", "https://a.test/api?a=2&a=1") != request_key(
        "GET", "https://a.test/api?a=1&a=2"
    )
    with pytest.raises(ValueError):
        request_key("GET", "https://secret@a.test/api")


@pytest.mark.parametrize("kind", ["duplicate", "length", "escape"])
def test_invalid_manifest(tmp_path, kind):
    make_set(tmp_path, b"body")
    path = tmp_path / "manifest.jsonl"
    row = json.loads(path.read_text())
    if kind == "length":
        row["bytes"] += 1
    if kind == "escape":
        row["file"] = "../outside.bin"
    text = json.dumps(row) + "\n"
    path.write_text(text * (2 if kind == "duplicate" else 1), encoding="utf-8")
    with pytest.raises(ValueError):
        FrozenSet(tmp_path)


def test_dst_25_hour_day_both_clients_and_frame(tmp_path):
    make_set(tmp_path, document(period(START, END, 100)))
    results = {mode: worker(tmp_path, mode) for mode in ("nb-records", "nb-frame", "entsoe-py")}
    for result in results.values():
        assert result["status"] == "PASS", result
        assert result["audit"]["records"] == 100
        assert result["audit"]["missing_quarters"] == 0
        assert result["audit"]["duplicate_intervals"] == 0
        assert result["audit"]["reordered_zones"] == 0
        assert result["audit"]["all_utc"]
        assert result["audit"]["first_utc"] == START.isoformat()
        assert result["audit"]["last_utc"] == (END - timedelta(minutes=15)).isoformat()
        assert result["request_mappings"] == []
    diff = differences(results["nb-records"]["rows"], results["entsoe-py"]["rows"])
    assert diff["max_abs_price_difference"] == 0
    assert diff["nb_only_intervals"] == diff["alternative_only_intervals"] == 0


def test_mixed_hourly_quarterly_native_frequencies(tmp_path):
    start = datetime(2022, 1, 1, tzinfo=UTC)
    split, end = start + timedelta(hours=1), start + timedelta(hours=2)
    make_set(
        tmp_path, document(period(start, split, 1, "PT60M"), period(split, end, 4)), start, end
    )
    nb = worker(tmp_path, "nb-records")
    alt = worker(tmp_path, "entsoe-py")
    assert nb["status"] == alt["status"] == "PASS", (nb, alt)
    assert nb["audit"]["records"] == 8
    assert nb["audit"]["source_resolutions_s"] == [900, 3600]
    assert alt["audit"]["records"] == 5
    assert differences(nb["rows"], alt["rows"])["nb_only_intervals"] == 3
    assert differences(nb["rows"], alt["rows"])["max_abs_price_difference"] == 0


def test_duplicate_points_rejected_by_nb_overwritten_by_alternative(tmp_path):
    end = START + timedelta(hours=1)
    make_set(tmp_path, document(period(START, end, 4, positions=[1, 1, 2, 3, 4])), START, end)
    rejected = worker(tmp_path, "nb-records")
    assert rejected["status"] == "ERROR"
    assert rejected["error_type"] == "SourceError"
    assert rejected["error"] == "entsoe: duplicate price for category A04"
    alternative = worker(tmp_path, "entsoe-py")
    assert alternative["status"] == "PASS", alternative
    assert alternative["audit"]["records"] == 4


@pytest.mark.parametrize("missing", ["category", "price"])
def test_optional_source_fields(tmp_path, missing):
    end = START + timedelta(hours=1)
    body = document(
        period(START, end, 4, category=missing != "category", missing_price=missing == "price")
    )
    make_set(tmp_path, body, START, end)
    assert worker(tmp_path, "nb-records")["status"] == "PASS"
    rejected = worker(tmp_path, "entsoe-py")
    assert rejected["status"] == "ERROR"
    assert rejected["error_type"] == "AttributeError"
    assert rejected["requests_used"]


def test_audit_detects_gaps_order_duplicate_null_and_price_difference():
    start = datetime(2025, 1, 1, tzinfo=UTC)

    def row(offset, value):
        return {
            "zone": "SE3",
            "start": (start + timedelta(minutes=15 * offset)).isoformat(),
            "values": {"price": value},
        }

    rows = [row(1, 2), row(0, None), row(1, 3)]
    result = audit(rows, start, start + timedelta(hours=1), [BiddingZone.SE3])
    assert result["missing_quarters"] == 2
    assert result["duplicate_intervals"] == 1
    assert result["reordered_zones"] == 1
    assert result["null_values"] == 1
    assert differences([row(1, 2)], [row(1, 3)])["max_abs_price_difference"] == 1
    assert summarize([{"wall_s": 1} for _ in range(7)])["wall_s"]["p95"] is None


def test_no_forged_chunk_match(tmp_path):
    make_set(tmp_path, document(period(START, END, 100)))
    frozen = FrozenSet(tmp_path)
    with frozen.requests() as session, pytest.raises(ReplayMiss):
        session.get(
            "https://web-api.tp.entsoe.eu/api",
            params={
                "documentType": "A85",
                "controlArea_Domain": AREA,
                "periodStart": "202510252200",
                "periodEnd": "202510262200",
            },
        )
    assert parameters(frozen)[1] == END


def test_esett_replay_uses_native_json_parser(tmp_path):
    end = START + timedelta(minutes=15)
    body = json.dumps(
        [
            {
                "mba": "SE3",
                "timestampUTC": START.isoformat().replace("+00:00", "Z"),
                "timestamp": "2025-10-26T00:00:00",
                "imblSalesPrice": 21.5,
                "imblPurchasePrice": 21.5,
                "upRegPrice": 21.5,
                "downRegPrice": 21.5,
                "mainDirRegPowerPerMBA": 1,
            }
        ]
    ).encode()
    make_set(tmp_path, body, START, end, provider="esett")
    nb, alt = worker(tmp_path, "nb-records"), worker(tmp_path, "esett-py")
    assert nb["status"] == alt["status"] == "PASS", (nb, alt)
    assert differences(nb["rows"], alt["rows"])["max_abs_price_difference"] == 0


def test_area_tables_match_client_identifiers():
    from benchmarks.areas import ENTSOE_AREAS, ESETT_AREAS
    from nordic_balancing.sources.entsoe import _EIC_CODES
    from nordic_balancing.sources.esett import _EIC_CODES as esett_codes

    assert {zone.value: code for zone, code in _EIC_CODES.items()} == ENTSOE_AREAS
    assert {zone.value: code for zone, code in esett_codes.items()} == ESETT_AREAS


def test_publication_oracle_preserves_period_gap(tmp_path):
    from benchmarks.source_audit import publication

    start = datetime(2022, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=3)
    body = document(
        period(start, start + timedelta(hours=1), 1, "PT60M"),
        period(start + timedelta(hours=2), end, 1, "PT60M"),
    )
    make_set(tmp_path, body, start, end)
    native, quarters, evidence = publication(FrozenSet(tmp_path))
    assert len(native) == 2
    assert len(quarters) == 8
    assert ("SE3", (start + timedelta(hours=1)).isoformat()) not in quarters
    assert evidence["explicit_duplicate_positions"] == 0


@pytest.mark.parametrize("amount", [42.0, None])
def test_publication_oracle_a03_continuation(tmp_path, amount):
    from benchmarks.source_audit import publication

    end = START + timedelta(hours=1)
    body = document(period(START, end, 4, positions=[1, 3]))
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        xml = archive.read("document.xml").replace(b"<curveType>A01", b"<curveType>A03")
    original = b"<Point><position>3</position><imbalance_Price.amount>21.5</imbalance_Price.amount>"
    replacement = b"<Point><position>3</position>"
    if amount is not None:
        replacement += f"<imbalance_Price.amount>{amount}</imbalance_Price.amount>".encode()
    assert original in xml
    xml = xml.replace(original, replacement)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("document.xml", xml)
    make_set(tmp_path, stream.getvalue(), START, end)
    native, quarters, _ = publication(FrozenSet(tmp_path))
    assert len(native) == len(quarters) == 4
    assert [quarters[key]["Long"] for key in sorted(quarters)] == [21.5, 21.5, amount, amount]
