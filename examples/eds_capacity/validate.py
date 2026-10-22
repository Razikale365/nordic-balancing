"""Compare seven recent UTC days of SE3 aFRR capacity from EDS and SvK.

Run from the checkout: uv run python examples/eds_capacity/validate.py --live
Without --live, importing or running this script fetches nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeAlias

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EnergiDataServiceClient,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
    SvKClient,
)

Key: TypeAlias = tuple[datetime, Direction]
Value: TypeAlias = tuple[float | None, float | None]
Hourly: TypeAlias = dict[Key, Value]


@dataclass(frozen=True, slots=True)
class HourValue:
    start: str
    direction: str
    price_eur_per_mw: float | None
    volume_mw: float | None


@dataclass(frozen=True, slots=True)
class Difference:
    start: str
    direction: str
    eds: Value
    svk: Value


@dataclass(frozen=True, slots=True)
class Comparison:
    matches: int
    shared_hours: int
    differences: list[Difference]
    eds_only: list[HourValue]
    svk_only: list[HourValue]
    errors: list[str]


def _hourly(records: list[ReserveCapacity]) -> Hourly:
    groups: dict[Key, list[ReserveCapacity]] = {}
    for record in records:
        if (
            record.zone is not BiddingZone.SE3
            or record.product is not ReserveProduct.AFRR
            or record.direction not in (Direction.UP, Direction.DOWN)
            or record.resolution != timedelta(hours=1)
            or record.price_eur_per_mw is None
            or record.volume_mw is None
        ):
            raise SourceError("comparison requires published hourly SE3 aFRR UP/DOWN values")
        hour = record.start.replace(minute=0, second=0, microsecond=0)
        groups.setdefault((hour, record.direction), []).append(record)
    result: Hourly = {}
    for key, quarters in groups.items():
        expected = {key[0] + q * INTERVAL for q in range(4)}
        values = {(r.price_eur_per_mw, r.volume_mw) for r in quarters}
        if len(quarters) != 4 or {r.start for r in quarters} != expected or len(values) != 1:
            raise SourceError(f"comparison requires four equal quarters for {key}")
        result[key] = values.pop()
    return result


def compare(eds_records: list[ReserveCapacity], svk_records: list[ReserveCapacity]) -> Comparison:
    eds, svk = _hourly(eds_records), _hourly(svk_records)
    shared = eds.keys() & svk.keys()
    differences = [
        Difference(hour.isoformat(), direction.name, eds[(hour, direction)], svk[(hour, direction)])
        for hour, direction in sorted(shared)
        if eds[(hour, direction)] != svk[(hour, direction)]
    ]
    eds_only = [
        HourValue(hour.isoformat(), direction.name, *eds[(hour, direction)])
        for hour, direction in sorted(eds.keys() - svk.keys())
    ]
    svk_only = [
        HourValue(hour.isoformat(), direction.name, *svk[(hour, direction)])
        for hour, direction in sorted(svk.keys() - eds.keys())
    ]
    errors = []
    if not shared:
        errors.append("no shared hour/directions; exact equality is unverified")
    if svk_only:
        errors.append("SvK has hour/directions absent from EDS")
    if any(r.volume_mw != 0.0 for r in eds_only):
        errors.append("EDS-only hour/directions include nonzero procurement")
    return Comparison(
        matches=len(shared) - len(differences),
        shared_hours=len({hour for hour, _ in shared}),
        differences=differences,
        eds_only=eds_only,
        svk_only=svk_only,
        errors=errors,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="required: fetch EDS, then SvK")
    parser.add_argument("--end", type=datetime.fromisoformat, help="exclusive UTC hour, ISO 8601")
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("results.json"))
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("this script calls live APIs; pass --live to run it")
    end = args.end or datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if end.tzinfo is None or end.utcoffset() != timedelta(0):
        parser.error("--end must be timezone-aware UTC")
    if end.minute or end.second or end.microsecond:
        parser.error("--end must be on the hour")
    start = end - timedelta(days=7)
    with EnergiDataServiceClient() as eds:
        eds_records = eds.capacity_market(ReserveProduct.AFRR, start, end, zones=[BiddingZone.SE3])
    with SvKClient() as svk:
        svk_records = svk.capacity_market(ReserveProduct.AFRR, start, end, zones=[BiddingZone.SE3])
    report = compare(eds_records, svk_records)
    if len(eds_records) != 7 * 24 * 4 * 2:
        report.errors.append("EDS must publish exactly one source row per hour for all 168 hours")
    result = {
        "status": "FAIL" if report.differences or report.errors else "PASS",
        "run_at": datetime.now(UTC).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "zone": "SE3",
        "product": "aFRR",
        "eds_only_hours": len({row.start for row in report.eds_only}),
        "eds_quarters": len(eds_records),
        "svk_quarters": len(svk_records),
        **asdict(report),
    }
    rendered = json.dumps(result, indent=2) + "\n"
    args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    print("wrote", args.out)
    return 1 if report.differences or report.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
