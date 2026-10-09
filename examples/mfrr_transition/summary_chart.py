"""One-line summary chart of the 2025-03-19 switch, sized for sharing.

Reads ``cache.json`` written by ``analysis.py`` (run that first) and plots, per UTC
day, the share of hours, across all 12 zones, whose four quarter-hour imbalance prices
are identical. Each day's value is drawn as a step covering that UTC day. The switch
came at 2025-03-19 00:00 CET, which is 2025-03-18T23:00Z, so the UTC day 18 March
already contains one changed hour in 11 of the 12 zones and shows 96 % instead of 100 %.
Writes ``summary.png`` next to this file.

    uv run --with matplotlib python examples/mfrr_transition/summary_chart.py
"""

import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

HERE = Path(__file__).parent
CACHE = HERE / "cache.json"
PNG = HERE / "summary.png"
EAM_GO_LIVE = datetime(2025, 3, 4, tzinfo=UTC)
QUARTER_PRICING = datetime(2025, 3, 18, 23, tzinfo=UTC)


def daily_uniform_share() -> tuple[list[date], list[float]]:
    quarters: dict[tuple[str, datetime], list[float | None]] = defaultdict(list)
    for start, zone, price in json.loads(CACHE.read_text(encoding="utf-8")):
        stamp = datetime.fromisoformat(start)
        quarters[(zone, stamp.replace(minute=0))].append(price)
    counts: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    for (_, hour), prices in quarters.items():
        if len(prices) == 4 and None not in prices:
            counts[hour.date()][0] += len(set(prices)) == 1
            counts[hour.date()][1] += 1
    days = sorted(counts)
    return days, [100 * counts[d][0] / counts[d][1] for d in days]


def main() -> None:
    if not CACHE.exists():
        raise SystemExit(f"{CACHE.name} not found: run analysis.py first")
    days, share = daily_uniform_share()
    # Each value covers its whole UTC day, so the drop is drawn at 19 March 00:00 UTC.
    edges = [datetime(d.year, d.month, d.day, tzinfo=UTC) for d in days]
    edges.append(edges[-1] + timedelta(days=1))
    fig, ax = plt.subplots(figsize=(12, 6.27), dpi=100)
    ax.stairs(share, mdates.date2num(edges), color="#0f5f8a", linewidth=3, baseline=None, zorder=3)
    ax.axvline(EAM_GO_LIVE, color="#6b7a86", linestyle=":", linewidth=2.2)
    ax.axvline(QUARTER_PRICING, color="#c2410c", linestyle="--", linewidth=2.2)
    ax.annotate(
        "4 Mar\nmFRR EAM\ngo-live",
        (EAM_GO_LIVE, 50),
        xytext=(-12, 0),
        textcoords="offset points",
        ha="right",
        va="center",
        color="#4b5863",
        fontsize=19,
    )
    ax.annotate(
        "19 Mar\n15-minute\nimbalance prices",
        (QUARTER_PRICING, 50),
        xytext=(12, 0),
        textcoords="offset points",
        ha="left",
        va="center",
        color="#c2410c",
        fontsize=19,
    )
    ax.set_ylim(0, 105)
    ax.set_ylabel("% of hours", fontsize=16)
    fig.suptitle(
        "Hours where all four quarter-hour imbalance prices match",
        x=0.01,
        ha="left",
        fontsize=22,
        fontweight="bold",
    )
    ax.set_title(
        "12 Nordic bidding zones, eSett open data, per UTC day",
        loc="left",
        fontsize=16,
        color="#4b5863",
    )
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.tick_params(labelsize=15)
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#d5dde2")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.text(
        0.99,
        0.01,
        "github.com/Razikale365/nordic-balancing",
        ha="right",
        color="#6b7a86",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(PNG)
    print(f"wrote {PNG.name}")


if __name__ == "__main__":
    main()
