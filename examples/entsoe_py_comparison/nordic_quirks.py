"""Live measurement of Nordic imbalance-price quirks via the keyless eSett API.

Three measurements, bounded to ~25 requests with a 1-second pause between calls:

  a. The 2023-05-21T22:00Z hourly -> 15-minute publication switch: resolution
     values either side of it, for SE3 and FI, one day on each side.
  b. The "repeated hourly prices" claim: share of hours in which all four
     quarter-hour imbalance prices are identical, per zone, before vs after
     2025-03-19 00:00 CET, when mFRR and imbalance pricing moved to 15 minutes
     (the mFRR EAM itself went live on 2025-03-04 with hourly prices). Sampled over
     the first week of every second month from 2023-07 to 2026-09.
  c. Unpublished intervals: the last ~6 hours for all 12 zones — how many
     intervals return ``imbalance_price_eur is None`` vs are missing entirely.

Run: uv run python examples/entsoe_py_comparison/nordic_quirks.py
Writes nordic_quirks_results.json next to this file.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nordic_balancing import BiddingZone, ESettClient, ImbalancePrice

SWITCH = datetime(2023, 5, 21, 22, tzinfo=UTC)
QUARTER_PRICING = datetime(2025, 3, 18, 23, tzinfo=UTC)  # 15-min mFRR + imbalance pricing
DAY = timedelta(days=1)
QUARTER = timedelta(minutes=15)
PAUSE = 1.0

requests_made = 0


def fetch(
    client: ESettClient,
    start: datetime,
    end: datetime,
    zones: list[BiddingZone],
) -> list[ImbalancePrice]:
    global requests_made
    if requests_made:
        time.sleep(PAUSE)
    prices = client.imbalance_prices(start, end, zones)
    requests_made += 1  # every window here is <= 31 days: one HTTP request
    return prices


def resolution_summary(prices: list[ImbalancePrice]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for price in prices:
        counts[str(price.resolution)] = counts.get(str(price.resolution), 0) + 1
    return counts


def sample_months() -> list[datetime]:
    months = []
    cursor = datetime(2023, 7, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    while cursor <= end:
        months.append(cursor)
        year, month = cursor.year, cursor.month + 2
        if month > 12:
            year, month = year + 1, month - 12
        cursor = datetime(year, month, 1, tzinfo=UTC)
    return months


def hour_key(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def main() -> None:
    results: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "eSett EXP14/Prices via nordic_balancing.ESettClient (keyless)",
    }
    with ESettClient() as client:
        # (a) The hourly -> 15-minute switch, one day on each side.
        print(f"(a) Publication-resolution switch at {SWITCH.isoformat()}")
        resolution_switch: dict[str, dict[str, dict[str, int]]] = {}
        for zone in (BiddingZone.SE3, BiddingZone.FI):
            before = fetch(client, SWITCH - DAY, SWITCH, [zone])
            after = fetch(client, SWITCH, SWITCH + DAY, [zone])
            resolution_switch[zone.value] = {
                "before": resolution_summary(before),
                "after": resolution_summary(after),
            }
            print(
                f"  {zone.value}: before={resolution_summary(before)} "
                f"after={resolution_summary(after)}"
            )
        results["resolution_switch"] = {
            "switch_utc": SWITCH.isoformat(),
            "zones": resolution_switch,
        }
        print()

        # (b) Identical quarter-hour prices within each hour.
        zones = list(BiddingZone)
        # per zone per period: [hours_with_4_quarters, uniform_hours, all_none_hours]
        tally: dict[str, dict[str, list[int]]] = {
            zone.value: {"before": [0, 0, 0], "after": [0, 0, 0]} for zone in zones
        }
        months = sample_months()
        print(f"(b) Identical quarter prices per hour, {len(months)} sample weeks")
        for month_start in months:
            prices = fetch(client, month_start, month_start + 7 * DAY, zones)
            by_hour: dict[tuple[str, datetime], list[float | None]] = {}
            for price in prices:
                key = (price.zone.value, hour_key(price.start))
                by_hour.setdefault(key, []).append(price.imbalance_price_eur)
            for (zone_value, hour), quarters in by_hour.items():
                if len(quarters) != 4:
                    continue
                period = "before" if hour < QUARTER_PRICING else "after"
                bucket = tally[zone_value][period]
                bucket[0] += 1
                if all(value is None for value in quarters):
                    bucket[2] += 1
                elif len(set(quarters)) == 1:
                    bucket[1] += 1
        print(f"  {'zone':<4} {'before share':>14} {'after share':>14}")
        per_zone: dict[str, dict[str, dict[str, float | int]]] = {}
        totals = {"before": [0, 0, 0], "after": [0, 0, 0]}
        for zone in zones:
            per_zone[zone.value] = {}
            row = []
            for period in ("before", "after"):
                hours, uniform, all_none = tally[zone.value][period]
                for index in range(3):
                    totals[period][index] += tally[zone.value][period][index]
                share = uniform / hours if hours else float("nan")
                per_zone[zone.value][period] = {
                    "hours": hours,
                    "uniform_hours": uniform,
                    "all_none_hours": all_none,
                    "uniform_share": share,
                }
                row.append(f"{share:>13.1%}")
            print(f"  {zone.value:<4} {row[0]} {row[1]}")
        overall = {}
        row = []
        for period in ("before", "after"):
            hours, uniform, all_none = totals[period]
            share = uniform / hours if hours else float("nan")
            overall[period] = {
                "hours": hours,
                "uniform_hours": uniform,
                "all_none_hours": all_none,
                "uniform_share": share,
            }
            row.append(f"{share:>13.1%}")
        print(f"  {'ALL':<4} {row[0]} {row[1]}")
        results["repeated_hourly"] = {
            "sample": "first week of every second month, 2023-07 to 2026-09",
            "boundary_utc": QUARTER_PRICING.isoformat(),
            "per_zone": per_zone,
            "overall": overall,
        }
        print()

        # (c) Unpublished intervals in the last ~6 hours, all zones.
        now = datetime.now(UTC)
        end = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        start = end - timedelta(hours=6)
        prices = fetch(client, start, end, zones)
        expected = int((end - start) / QUARTER)
        print(f"(c) Unpublished intervals, {start.isoformat()} .. {end.isoformat()}")
        print(f"  {'zone':<4} {'rows':>5} {'expected':>9} {'None':>5} {'missing':>8}")
        unpublished: dict[str, dict[str, int]] = {}
        for zone in zones:
            rows = [p for p in prices if p.zone == zone]
            none_count = sum(1 for p in rows if p.imbalance_price_eur is None)
            missing = expected - len(rows)
            unpublished[zone.value] = {
                "rows": len(rows),
                "expected": expected,
                "price_none": none_count,
                "missing": missing,
            }
            print(f"  {zone.value:<4} {len(rows):>5} {expected:>9} {none_count:>5} {missing:>8}")
        results["unpublished"] = {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "per_zone": unpublished,
        }

    results["requests_made"] = requests_made
    out = Path(__file__).with_name("nordic_quirks_results.json")
    out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"{requests_made} requests; raw numbers -> {out.name}")


if __name__ == "__main__":
    main()
