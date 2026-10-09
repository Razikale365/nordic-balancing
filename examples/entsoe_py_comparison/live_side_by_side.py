"""Live side-by-side diff: entsoe-py vs nordic-balancing, once a token exists.

Fetches imbalance prices for SE3 and FI on 2026-01-15 (a local day in each
zone's market timezone) three ways and diffs timestamps and values:

  * entsoe-py ``EntsoePandasClient.query_imbalance_prices`` (ENTSO-E A85)
  * nordic-balancing ``EntsoeClient`` (ENTSO-E A85, same endpoint)
  * nordic-balancing ``ESettClient`` (keyless eSett EXP14)

Exits cleanly with "ENTSOE_API_KEY not set" when no token is configured, so it
is safe to run today.

Run: uv run --with entsoe-py==0.8.1 python examples/entsoe_py_comparison/live_side_by_side.py
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DAY = datetime(2026, 1, 15)
AREAS = {  # nordic zone -> (entsoe-py country code, market timezone)
    "SE3": ("SE_3", "Europe/Stockholm"),
    "FI": ("FI", "Europe/Helsinki"),
}


def flatten_entsoe_py(df) -> dict[datetime, set[float]]:
    """Map UTC timestamp -> set of non-NaN prices across category columns."""
    points: dict[datetime, set[float]] = {}
    for stamp, row in df.iterrows():
        values = {float(value) for value in row if value == value}
        if values:
            points[stamp.tz_convert("UTC").to_pydatetime()] = values
    return points


def flatten_nordic(prices) -> dict[datetime, set[float]]:
    """Map UTC timestamp -> set with the single imbalance price (empty if None)."""
    points: dict[datetime, set[float]] = {}
    for price in prices:
        values = set()
        if price.imbalance_price_eur is not None:
            values.add(price.imbalance_price_eur)
        points[price.start] = values
    return points


def diff(
    label: str,
    left_name: str,
    left: dict[datetime, set[float]],
    right_name: str,
    right: dict[datetime, set[float]],
) -> None:
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    mismatched = sorted(stamp for stamp in set(left) & set(right) if left[stamp] != right[stamp])
    print(f"  {left_name} vs {right_name} ({label}):")
    print(f"    rows: {len(left)} vs {len(right)}")
    print(f"    only in {left_name}: {len(only_left)} {only_left[:3]}")
    print(f"    only in {right_name}: {len(only_right)} {only_right[:3]}")
    print(
        f"    differing values: {len(mismatched)} "
        f"{[(t.isoformat(), sorted(left[t]), sorted(right[t])) for t in mismatched[:3]]}"
    )


def main() -> int:
    api_key = os.environ.get("ENTSOE_API_KEY")
    if not api_key:
        print("ENTSOE_API_KEY not set")
        return 0

    import pandas as pd
    from entsoe import EntsoePandasClient

    from nordic_balancing import BiddingZone, EntsoeClient, ESettClient

    pandas_client = EntsoePandasClient(api_key=api_key)
    esett = ESettClient()
    entsoe = EntsoeClient(api_key=api_key)
    try:
        for zone_value, (country_code, tz_name) in AREAS.items():
            zone = BiddingZone[zone_value]
            start = DAY.replace(tzinfo=ZoneInfo(tz_name))
            end = start + timedelta(days=1)
            print(f"{zone_value} {start.date()} ({tz_name})")

            df = pandas_client.query_imbalance_prices(
                country_code, start=pd.Timestamp(start), end=pd.Timestamp(end)
            )
            py_points = flatten_entsoe_py(df)
            entsoe_points = flatten_nordic(entsoe.imbalance_prices(start, end, [zone]))
            esett_points = flatten_nordic(esett.imbalance_prices(start, end, [zone]))

            diff("A85 vs A85", "entsoe-py", py_points, "nb-entsoe", entsoe_points)
            diff("eSett vs ENTSO-E", "esett", esett_points, "nb-entsoe", entsoe_points)
            print()
    finally:
        esett.close()
        entsoe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
