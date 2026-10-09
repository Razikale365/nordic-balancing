"""One reproducible view of when Nordic imbalance prices became quarter-hourly.

Nordic imbalance settlement has been 15-minute since 2023-05-22, and the mFRR
energy activation market went live on 2025-03-04 with hourly pricing. Imbalance
prices only became genuinely quarter-hourly when mFRR and imbalance pricing moved
to 15 minutes at 2025-03-19 00:00 CET (2025-03-18T23:00Z). This script fetches eSett
imbalance prices for all 12 zones, 2024-12-01 to 2025-06-01 UTC (cached as
``cache.json`` after the first run), and shows per UTC day:

  - the share of hours where all four quarter-hour prices are identical;
  - the mean intra-hour price range (max - min of the four quarters).

Run: uv run --with matplotlib python examples/mfrr_transition/analysis.py
Writes mfrr_transition.png next to this file; cache.json is git-ignored.
"""

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from nordic_balancing import BiddingZone, ESettClient

START = datetime(2024, 12, 1, tzinfo=UTC)
END = datetime(2025, 6, 1, tzinfo=UTC)
EAM_GOLIVE = datetime(2025, 3, 4, tzinfo=UTC)  # mFRR EAM live, prices still hourly
SWITCH = datetime(2025, 3, 18, 23, tzinfo=UTC)  # 15-minute mFRR and imbalance pricing
HERE = Path(__file__).parent
CACHE = HERE / "cache.json"
PNG = HERE / "mfrr_transition.png"


def load_rows() -> list[tuple[datetime, str, float | None]]:
    """(start, zone, price) rows for the window, from cache.json or one fetch."""
    if CACHE.exists():
        print(f"reusing {CACHE.name} (no network calls)")
        return [
            (datetime.fromisoformat(start), zone, price)
            for start, zone, price in json.loads(CACHE.read_text(encoding="utf-8"))
        ]
    with ESettClient() as client:  # ~6 chunked requests for this window
        prices = client.imbalance_prices(START, END, list(BiddingZone))
    rows = [(p.start, p.zone.value, p.imbalance_price_eur) for p in prices]
    payload = [[start.isoformat(), zone, price] for start, zone, price in rows]
    CACHE.write_text(json.dumps(payload), encoding="utf-8")
    print(f"fetched {len(rows)} rows from eSett -> {CACHE.name}")
    return rows


def hourly_stats(
    rows: list[tuple[datetime, str, float | None]],
) -> dict[tuple[str, datetime], tuple[bool, float]]:
    """Per (zone, hour): (all 4 quarters identical, intra-hour max - min)."""
    by_hour: dict[tuple[str, datetime], list[float | None]] = defaultdict(list)
    for start, zone, price in rows:
        by_hour[(zone, start.replace(minute=0, second=0, microsecond=0))].append(price)
    stats = {}
    for key, quarters in by_hour.items():
        if len(quarters) == 4 and None not in quarters:
            stats[key] = (
                len(set(quarters)) == 1,
                max(quarters) - min(quarters),  # type: ignore[type-var]
            )
    return stats


def plot(hourly: dict[tuple[str, datetime], tuple[bool, float]], zones: list[str]) -> None:
    """Two panels vs UTC day: identical-quarter share and mean intra-hour range."""
    weekly: dict[tuple[str, date], list[tuple[bool, float]]] = defaultdict(list)
    for (zone, hour), values in hourly.items():
        week = hour.date()
        weekly[(zone, week)].append(values)
    colors = matplotlib.colormaps["tab20"]
    fig, (ax_share, ax_range) = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for index, zone in enumerate(zones):
        weeks = sorted(week for z, week in weekly if z == zone)
        days = [datetime.combine(week, datetime.min.time(), tzinfo=UTC) for week in weeks]
        points = [weekly[(zone, week)] for week in weeks]
        ax_share.plot(
            days,
            [sum(u for u, _ in p) / len(p) for p in points],
            color=colors(index),
            linewidth=0.9,
            label=zone,
        )
        ax_range.plot(
            days,
            [sum(r for _, r in p) / len(p) for p in points],
            color=colors(index),
            linewidth=0.9,
            label=zone,
        )
    for axis, ylabel in (
        (ax_share, "hours with 4 identical\nquarter prices (share)"),
        (ax_range, "mean intra-hour range\n(EUR/MWh)"),
    ):
        axis.axvline(EAM_GOLIVE, color="grey", linestyle=":", linewidth=1)
        axis.axvline(SWITCH, color="black", linestyle="--", linewidth=1)
        axis.set_ylabel(ylabel)
        axis.grid(alpha=0.3)
    ax_share.set_ylim(-0.02, 1.02)
    ax_share.set_title(
        "Quarter-hour imbalance prices start to differ on 2025-03-19 (dashed); "
        "EAM go-live 2025-03-04 (dotted)"
    )
    ax_share.legend(ncol=6, fontsize=8, loc="upper right")
    ax_range.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.tight_layout()
    fig.savefig(PNG, dpi=110)
    print(f"plot -> {PNG.name} ({PNG.stat().st_size / 1024:.0f} KiB)")


def print_table(hourly: dict[tuple[str, datetime], tuple[bool, float]], zones: list[str]) -> None:
    """Per zone: identical-quarter share and mean range before/after the 15-minute switch."""
    print(f"\n{'zone':<5} {'uniform %':>19} {'mean intra-hour range':>25}")
    print(f"{'':<5} {'before':>9} {'after':>9} {'before':>12} {'after':>12}")
    for zone in zones:
        cells = []
        for period in ("before", "after"):
            values = [
                v
                for (z, hour), v in hourly.items()
                if z == zone and (hour < SWITCH) == (period == "before")
            ]
            uniform = sum(u for u, _ in values) / len(values)
            mean_range = sum(r for _, r in values) / len(values)
            cells.append((uniform, mean_range))
        print(
            f"{zone:<5} {cells[0][0]:>8.1%} {cells[1][0]:>8.1%}"
            f" {cells[0][1]:>11.2f} {cells[1][1]:>11.2f}"
        )


def main() -> None:
    rows = load_rows()
    zones = sorted(zone.value for zone in BiddingZone)
    hourly = hourly_stats(rows)
    plot(hourly, zones)
    print_table(hourly, zones)


if __name__ == "__main__":
    main()
