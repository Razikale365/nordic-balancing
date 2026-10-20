"""Offline tests for the example scripts under examples/ (no network)."""

import importlib.util
import json
import runpy
import socket
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fakes import (
    EDS_CAPACITY_ROW,
    FI_NO_END,
    FI_NO_FIXTURES,
    FI_NO_START,
    eds_client,
    eds_page,
    fi_no_entsoe_fetch,
    fi_no_fingrid_fetch,
    fi_no_xml_member,
    http_client,
    zip_members,
)

from nordic_balancing import (
    BiddingZone,
    Direction,
    EnergiDataServiceClient,
    EntsoeClient,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
)

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


START = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)
END = START + timedelta(minutes=30)
HOUR_START = START.replace(minute=0)


@pytest.fixture
def capacity_validation(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    def forbid_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("capacity validation import must not open a connection")

    monkeypatch.setattr("socket.socket.connect", forbid_network)
    path = Path(__file__).parents[1] / "examples" / "eds_capacity" / "validate.py"
    spec = importlib.util.spec_from_file_location("eds_capacity_validation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def se3_capacity() -> list[ReserveCapacity]:
    row = {**EDS_CAPACITY_ROW, "PriceArea": "SE3"}
    return eds_client(lambda _: eds_page([row])).capacity_market(
        ReserveProduct.AFRR, HOUR_START, END, zones=[BiddingZone.SE3]
    )


HOUR = timedelta(hours=1)


# --- Example: ENTSO-E validation ---------------------------------------------------------------


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

    def fake_fetch(
        start: datetime, end: datetime, zones: Any = None
    ) -> tuple[dict[Any, Any], dict[Any, Any]]:
        assert zones is None
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


def test_multi_chunk_mode_limits_zones_and_compares_the_seam(
    validate: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[datetime, datetime, Any]] = []
    seam_start = validate.SEAMS["multi-chunk"][0]

    def fake_fetch(
        start: datetime, end: datetime, zones: Any = None
    ) -> tuple[dict[Any, Any], dict[Any, Any]]:
        calls.append((start, end, zones))
        if start == seam_start:
            return {("SE3", seam_start): (1.0, 15)}, {}
        return {("SE3", seam_start): (1.0, 15), ("SE3", start): (2.0, 60)}, {
            ("SE3", start): (2.0, 15)
        }

    monkeypatch.setattr(validate, "fetch", fake_fetch)
    out = tmp_path / "out.json"
    assert validate.main(["multi-chunk", "--live", "--out", str(out)]) == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert [zones for *_, zones in calls] == [("DK1", "SE3"), ("DK1", "SE3")]
    (window,) = result["windows"]
    assert window["seam"]["identical"] is True
    assert (
        window["seam"]["multi_chunk_intervals"] == window["seam"]["single_request_intervals"] == 1
    )
    assert window["resolution_label_differs"] == 1


# --- Example: EDS capacity comparison ----------------------------------------------------------


def test_capacity_comparison_counts_hours_once_and_explains_zero_volume_omissions(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity]
) -> None:
    svk = [replace(r, source="svk") for r in se3_capacity if r.direction is Direction.UP]
    report = capacity_validation.compare(se3_capacity, svk)
    assert report.matches == 1
    assert report.shared_hours == 1
    assert report.differences == []
    assert len(report.eds_only) == 1
    assert report.eds_only[0].direction == "DOWN"
    assert report.eds_only[0].volume_mw == 0.0
    assert report.svk_only == []
    assert report.errors == []


@pytest.mark.parametrize("change_price", [True, False], ids=["price_eur_per_mw", "volume_mw"])
def test_capacity_comparison_reports_exact_differences(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity], change_price: bool
) -> None:
    svk = [
        replace(
            r,
            source="svk",
            price_eur_per_mw=0.001 if change_price else r.price_eur_per_mw,
            volume_mw=r.volume_mw if change_price else 0.001,
        )
        for r in se3_capacity
    ]
    report = capacity_validation.compare(se3_capacity, svk)
    assert report.matches == 0
    assert len(report.differences) == 2
    assert report.differences[0].start == HOUR_START.isoformat()
    assert report.differences[0].eds != report.differences[0].svk


def test_capacity_comparison_rejects_incomplete_or_inconsistent_quarters(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity]
) -> None:
    with pytest.raises(SourceError, match="four equal quarters"):
        capacity_validation.compare(se3_capacity[:-1], se3_capacity)
    changed = [replace(se3_capacity[0], volume_mw=1.0), *se3_capacity[1:]]
    with pytest.raises(SourceError, match="four equal quarters"):
        capacity_validation.compare(changed, se3_capacity)


def test_capacity_comparison_does_not_hide_missing_eds_or_nonzero_eds_only(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity]
) -> None:
    down = [r for r in se3_capacity if r.direction is Direction.DOWN]
    eds_only_report = capacity_validation.compare(se3_capacity, down)
    assert eds_only_report.errors
    missing_report = capacity_validation.compare(down, se3_capacity)
    assert len(missing_report.svk_only) == 1
    assert missing_report.errors


def test_capacity_comparison_requires_live_flag_before_fetch(
    capacity_validation: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the script must not fetch without --live")

    monkeypatch.setattr(EnergiDataServiceClient, "capacity_market", forbidden)
    with pytest.raises(SystemExit) as caught:
        capacity_validation.main([])
    assert caught.value.code == 2


def test_capacity_comparison_matches_both_directions_without_counting_quarters(
    capacity_validation: ModuleType, se3_capacity: list[ReserveCapacity]
) -> None:
    report = capacity_validation.compare(se3_capacity, se3_capacity)
    assert report.matches == 2
    assert report.shared_hours == 1
    assert not (report.differences or report.eds_only or report.svk_only or report.errors)


def test_capacity_comparison_refuses_to_pass_without_shared_hours(
    capacity_validation: ModuleType,
) -> None:
    report = capacity_validation.compare([], [])
    assert report.matches == 0
    assert report.shared_hours == 0
    assert report.errors == ["no shared hour/directions; exact equality is unverified"]


# --- Example: Finland and Norway capacity cross-check ------------------------------------------


def test_evidence_comparison_detects_gaps_differences_and_empty_windows() -> None:
    compare = runpy.run_path(
        str(FI_NO_FIXTURES.parents[2] / "examples" / "fi_no_capacity" / "cross_check.py")
    )["compare"]
    fg = fi_no_fingrid_fetch(ReserveProduct.MFRR)
    en = fi_no_entsoe_fetch(zone=BiddingZone.FI)
    result = compare(en, fg, BiddingZone.FI, FI_NO_START, FI_NO_END)
    assert result["status"] == "PASS"
    assert result["matched_hour_directions"] == 48
    result = compare(en[8:], fg[8:], BiddingZone.FI, FI_NO_START, FI_NO_END)
    assert result["status"] == "FAIL"
    assert len(result["missing_hour_directions"]) == 2
    assert compare([], [], BiddingZone.FI, FI_NO_START, FI_NO_END)["status"] == "FAIL"
    from dataclasses import replace

    changed = [
        replace(r, price_eur_per_mw=999.0)
        if r.start < FI_NO_START + HOUR and r.direction is Direction.UP
        else r
        for r in en
    ]
    result = compare(changed, fg, BiddingZone.FI, FI_NO_START, FI_NO_END)
    assert result["status"] == "FAIL"
    assert len(result["differing_hour_directions"]) == 1


def test_evidence_script_requires_live_opt_in() -> None:
    main = runpy.run_path(
        str(FI_NO_FIXTURES.parents[2] / "examples" / "fi_no_capacity" / "cross_check.py")
    )["main"]
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_evidence_binary_search_checks_first_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    first_day = runpy.run_path(
        str(FI_NO_FIXTURES.parents[2] / "examples" / "fi_no_capacity" / "cross_check.py")
    )["first_day"]
    records = fi_no_entsoe_fetch(zone=BiddingZone.FI)
    calls: list[datetime] = []
    boundary = datetime(2022, 12, 1, tzinfo=UTC)

    def available(
        product: ReserveProduct, start: datetime, end: datetime, *, zones: list[BiddingZone]
    ) -> list[ReserveCapacity]:
        assert product is ReserveProduct.MFRR and zones == [BiddingZone.FI]
        assert end - start == timedelta(days=1)
        calls.append(start)
        return records if start >= boundary else []

    with EntsoeClient("test-token") as client:
        monkeypatch.setattr(client, "capacity_market", available)
        assert (
            first_day(
                client,
                BiddingZone.FI,
                datetime(2010, 1, 1, tzinfo=UTC),
                FI_NO_START.replace(hour=0),
            )
            == boundary
        )
    assert boundary in calls and boundary - timedelta(days=1) in calls
    assert len(calls) < 20


def test_evidence_se3_uses_validated_a47_parser() -> None:
    fetch = runpy.run_path(
        str(FI_NO_FIXTURES.parents[2] / "examples" / "fi_no_capacity" / "cross_check.py")
    )["_entsoe_se3"]
    xml = fi_no_xml_member(BiddingZone.FI).replace(b"10YFI-1--------U", b"10Y1001A1001A46L")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=zip_members(xml, xml))

    with (
        http_client(handler) as http,
        EntsoeClient("test-token", http) as client,
    ):
        result = fetch(client, FI_NO_START, FI_NO_END)
    assert len(result) == 192
    assert all(r.zone is BiddingZone.SE3 and r.product is ReserveProduct.MFRR for r in result)
    assert requests[0].url.params["processType"] == "A47"
    assert requests[0].url.params["controlArea_Domain"] == "10Y1001A1001A46L"
