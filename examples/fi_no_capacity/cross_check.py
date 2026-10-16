"""Compare FI/SE3 mFRR capacity, or find NO/FI A47 coverage (explicit --live).

Example: uv run python examples/fi_no_capacity/cross_check.py --live \
    --start 2026-10-07T22:00Z --end 2026-10-08T22:00Z
Nothing is fetched on import or without --live.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EntsoeClient,
    FingridClient,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
    SvKClient,
)
from nordic_balancing.sources.entsoe import _jobs

type Key = tuple[datetime, Direction]
type Value = tuple[float | None, float | None]
HOUR = timedelta(hours=1)


def _hourly(records: list[ReserveCapacity], zone: BiddingZone) -> dict[Key, Value]:
    groups: dict[Key, list[ReserveCapacity]] = {}
    for record in records:
        if (
            record.zone is not zone
            or record.product is not ReserveProduct.MFRR
            or record.direction not in (Direction.UP, Direction.DOWN)
            or record.resolution != HOUR
            or record.price_eur_per_mw is None
            or record.volume_mw is None
        ):
            raise SourceError("comparison requires published hourly mFRR UP/DOWN values")
        hour = record.start.replace(minute=0, second=0, microsecond=0)
        groups.setdefault((hour, record.direction), []).append(record)
    result: dict[Key, Value] = {}
    for key, quarters in groups.items():
        expected = {key[0] + q * INTERVAL for q in range(4)}
        values = {(r.price_eur_per_mw, r.volume_mw) for r in quarters}
        if len(quarters) != 4 or {r.start for r in quarters} != expected or len(values) != 1:
            raise SourceError(f"comparison requires four equal quarters for {key}")
        result[key] = values.pop()
    return result


def compare(
    entsoe_records: list[ReserveCapacity],
    tso_records: list[ReserveCapacity],
    zone: BiddingZone,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    en, tso = _hourly(entsoe_records, zone), _hourly(tso_records, zone)
    expected = {
        (start + hour * HOUR, direction)
        for hour in range((end - start) // HOUR)
        for direction in (Direction.UP, Direction.DOWN)
    }
    shared = en.keys() & tso.keys()
    differing = [
        {
            "start": stamp.isoformat(),
            "direction": direction.name,
            "entsoe": en[(stamp, direction)],
            "tso": tso[(stamp, direction)],
        }
        for stamp, direction in sorted(shared)
        if en[(stamp, direction)] != tso[(stamp, direction)]
    ]
    missing = [
        {
            "start": stamp.isoformat(),
            "direction": direction.name,
            "entsoe_missing": (stamp, direction) not in en,
            "tso_missing": (stamp, direction) not in tso,
        }
        for stamp, direction in sorted(expected - shared)
    ]
    unexpected = [
        {"start": stamp.isoformat(), "direction": direction.name}
        for stamp, direction in sorted((en.keys() | tso.keys()) - expected)
    ]
    return {
        "zone": zone.value,
        "status": "PASS"
        if expected and not differing and not missing and not unexpected
        else "FAIL",
        "matched_hour_directions": len(shared) - len(differing),
        "differing_hour_directions": differing,
        "missing_hour_directions": missing,
        "unexpected_hour_directions": unexpected,
    }


def _entsoe_se3(client: EntsoeClient, start: datetime, end: datetime) -> list[ReserveCapacity]:
    # SE3 is an evidence-only comparison outside the public NO/FI contract.
    # Use the same validated parser and chunk planning without widening that API.
    records: dict[Key, ReserveCapacity] = {}
    for job in _jobs(start, end, [BiddingZone.SE3]):
        for record in client._fetch_capacity(*job, product=ReserveProduct.MFRR):
            if not start <= record.start < end:
                continue
            key = record.start, record.direction
            old = records.get(key)
            if old is not None and (old.price_eur_per_mw, old.volume_mw) != (
                record.price_eur_per_mw,
                record.volume_mw,
            ):
                raise SourceError("conflicting SE3 capacity documents")
            records.setdefault(key, record)
    return list(records.values())


def first_day(
    client: EntsoeClient, zone: BiddingZone, lower: datetime, upper: datetime
) -> datetime:
    """Binary search daily availability; verify the boundary on adjacent days.

    Assumes a single historical coverage boundary, not arbitrary historical gaps.
    The returned UTC day can include the evening start of a local delivery day.
    """

    def available(day: datetime) -> bool:
        return bool(
            client.capacity_market(ReserveProduct.MFRR, day, day + timedelta(days=1), zones=[zone])
        )

    if not available(upper):
        raise SourceError(f"no {zone} data on the upper search day")
    if available(lower):
        raise SourceError(
            f"{zone} lower bound already has data; choose an earlier --coverage-lower"
        )
    low, high = lower, upper
    while (high - low).days > 1:
        mid = low + timedelta(days=(high - low).days // 2)
        if available(mid):
            high = mid
        else:
            low = mid
    if not available(high) or available(high - timedelta(days=1)):
        raise SourceError(f"{zone} coverage boundary failed adjacent-day verification")
    return high


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="required: enables source requests")
    parser.add_argument("--start", type=datetime.fromisoformat)
    parser.add_argument("--end", type=datetime.fromisoformat)
    parser.add_argument("--find-coverage", action="store_true")
    parser.add_argument(
        "--coverage-lower", type=datetime.fromisoformat, default=datetime(2010, 1, 1, tzinfo=UTC)
    )
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("this script calls live APIs; pass --live to run it")
    end = args.end or datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=7)
    start = args.start or end - timedelta(days=1)
    for name, value in (("start", start), ("end", end), ("coverage-lower", args.coverage_lower)):
        if (
            value.tzinfo is None
            or value.utcoffset() != timedelta(0)
            or value.minute
            or value.second
            or value.microsecond
        ):
            parser.error(f"--{name} must be an aware UTC hour")
    if end <= start:
        parser.error("--end must be after --start")
    with EntsoeClient() as en:
        try:
            if args.find_coverage:
                lower = args.coverage_lower
                upper = end.replace(hour=0)
                if lower.hour or lower >= upper:
                    parser.error("coverage bounds must be ordered UTC days")
                result = {
                    zone.value: first_day(en, zone, lower, upper).date().isoformat()
                    for zone in (
                        BiddingZone.NO1,
                        BiddingZone.NO2,
                        BiddingZone.NO3,
                        BiddingZone.NO4,
                        BiddingZone.NO5,
                        BiddingZone.FI,
                    )
                }
                print(
                    json.dumps(
                        {
                            "first_utc_day_with_data": result,
                            "assumption": (
                                "single historical availability boundary; adjacent days checked"
                            ),
                        },
                        indent=2,
                    )
                )
                return 0
            fi = en.capacity_market(ReserveProduct.MFRR, start, end, zones=[BiddingZone.FI])
            se3 = _entsoe_se3(en, start, end)
        except SourceError as exc:
            # The evidence-only SE3 path bypasses the public error wrapper.
            raise SourceError(str(exc).replace(en._api_key, "[REDACTED_SECRET]")) from None
    with FingridClient() as fg:
        fi_tso = fg.capacity_market(ReserveProduct.MFRR, start, end)
    with SvKClient() as svk:
        se3_tso = svk.capacity_market(ReserveProduct.MFRR, start, end, zones=[BiddingZone.SE3])
    reports = [
        compare(fi, fi_tso, BiddingZone.FI, start, end),
        compare(se3, se3_tso, BiddingZone.SE3, start, end),
    ]
    print(
        json.dumps(
            {"start": start.isoformat(), "end": end.isoformat(), "comparisons": reports}, indent=2
        )
    )
    return int(any(report["status"] != "PASS" for report in reports))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
