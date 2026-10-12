"""Offline tests for examples/entsoe_validation/validate.py (no network)."""

import importlib.util
import json
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "examples" / "entsoe_validation" / "validate.py"
T0 = datetime(2025, 4, 2, tzinfo=UTC)
Q = timedelta(minutes=15)


@pytest.fixture(scope="module")
def validate() -> Iterator[ModuleType]:
    # Importing the script must not open a connection.
    original = socket.socket.connect

    def refuse(*_: Any) -> None:
        raise AssertionError("network access during import")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    try:
        spec = importlib.util.spec_from_file_location("entsoe_validation", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]
    yield module


@pytest.mark.parametrize(
    ("entsoe", "esett", "expected"),
    [
        (21.0, 21.0, "exact"),
        (17.53, 17.52, "rounding"),  # 0.01 apart, above 0.01 only by float noise
        (17.54, 17.52, "material"),
        (0.0, 0.01, "rounding"),
        (0.0, 45.3, "zero_price"),
        (45.3, 0.0, "zero_price"),
        (-34.04, -59.6, "material"),
        (None, None, "both_unpublished"),
        (None, 0.0, "entsoe_unpublished"),
        (0.0, None, "esett_unpublished"),
    ],
)
def test_one_interval_falls_in_one_category(
    validate: ModuleType, entsoe: float | None, esett: float | None, expected: str
) -> None:
    assert validate.classify_one(entsoe, esett, 0.01) == expected


def test_every_key_in_the_union_is_counted_once(validate: ModuleType) -> None:
    entsoe = {
        ("DK1", T0): (10.0, 15),
        ("DK1", T0 + Q): (0.0, 15),
        ("DK1", T0 + 2 * Q): (None, 15),
        ("FI", T0): (5.0, 60),
        ("FI", T0 + 3 * Q): (7.0, 15),
    }
    esett = {
        ("DK1", T0): (10.01, 15),
        ("DK1", T0 + Q): (80.0, 15),
        ("DK1", T0 + 2 * Q): (3.0, 15),
        ("DK1", T0 + 3 * Q): (3.0, 15),
        ("DK1", T0 + 4 * Q): (3.0, 15),
        ("DK1", T0 + 6 * Q): (3.0, 15),
        ("FI", T0): (5.0, 15),
    }
    summary = validate.summarise(entsoe, esett)
    assert summary["keys"] == 8
    assert sum(summary["totals"].values()) == summary["keys"]
    assert summary["totals"] == {
        "missing_in_entsoe": 3,
        "missing_in_esett": 1,
        "both_unpublished": 0,
        "entsoe_unpublished": 1,
        "esett_unpublished": 0,
        "exact": 1,
        "rounding": 1,
        "zero_price": 1,
        "material": 0,
    }
    assert summary["per_zone"]["FI"]["exact"] == 1
    assert summary["per_zone"]["FI"]["missing_in_esett"] == 1
    # A label difference is an attribute of a compared interval, not a category.
    assert summary["resolution_label_differs"] == 1
    assert summary["missing_runs"]["missing_in_entsoe"] == {
        "DK1": [
            [(T0 + 3 * Q).isoformat(), (T0 + 4 * Q).isoformat()],
            [(T0 + 6 * Q).isoformat(), (T0 + 6 * Q).isoformat()],
        ]
    }
    assert [(d["zone"], d["category"]) for d in summary["differences"]] == [
        ("DK1", "rounding"),
        ("DK1", "zero_price"),
        ("DK1", "entsoe_unpublished"),
    ]


def test_without_live_it_refuses_before_any_request(
    validate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_fetch(*_: Any) -> None:
        raise AssertionError("fetched without --live")

    monkeypatch.setattr(validate, "fetch", no_fetch)
    with pytest.raises(SystemExit) as exit_:
        validate.main(["samples", "--out", str(tmp_path / "out.json")])
    assert exit_.value.code == 2
    assert not (tmp_path / "out.json").exists()


def test_live_run_writes_machine_readable_totals(
    validate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[datetime, datetime]] = []

    def fake_fetch(start: datetime, end: datetime) -> tuple[dict[Any, Any], dict[Any, Any]]:
        calls.append((start, end))
        return {("SE3", start): (1.0, 15)}, {
            ("SE3", start): (1.0, 15),
            ("SE3", start + Q): (2.0, 15),
        }

    monkeypatch.setattr(validate, "fetch", fake_fetch)
    out = tmp_path / "out.json"
    assert validate.main(["samples", "--live", "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert len(calls) == len(result["windows"]) == 5
    assert result["totals"]["exact"] == 5
    assert result["totals"]["missing_in_entsoe"] == 5
    assert all(sum(w["totals"].values()) == w["keys"] for w in result["windows"])
