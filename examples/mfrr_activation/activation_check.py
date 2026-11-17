"""Check how Fingrid's mFRR activation datasets can be read, for one 48-hour window.

Fetches 375 (activated mFRR balancing regulation, upward sum), 376 (downward sum),
369 (dominating direction) and 319 (imbalance price) for [2026-06-17T00Z, 2026-06-19T00Z)
with ``FingridClient.series`` and prints what the data says about its own conventions.
Needs ``FINGRID_API_KEY`` in the environment; four requests, paced by the client.
Nothing is fetched on import or without --live.

Run: uv run python examples/mfrr_activation/activation_check.py --live
"""

from __future__ import annotations

import argparse
import statistics
from datetime import UTC, datetime

from nordic_balancing import FingridClient

START = datetime(2026, 6, 17, tzinfo=UTC)
END = datetime(2026, 6, 19, tzinfo=UTC)


def main() -> None:
    with FingridClient() as fingrid:
        series = {ds: dict(fingrid.series(ds, START, END)) for ds in (375, 376, 369, 319)}
    quarters = sorted(series[375].keys() & series[376].keys() & series[369].keys())
    up, down, direction, price = (series[ds] for ds in (375, 376, 369, 319))
    n = len(quarters)
    print(f"window {START:%Y-%m-%dT%H:%MZ} to {END:%Y-%m-%dT%H:%MZ}: {n} quarters")

    print("\nSign of the 'downward' dataset 376")
    values = [v for v in down.values() if v is not None]
    naive, correct = sum(v > 0 for v in values), sum(v < 0 for v in values)
    print(f"  values > 0: {naive}, values < 0: {correct}")
    print(f"  down quarters found by the naive 'value > 0': {naive}; by 'value < 0': {correct}")

    def cls(t: datetime) -> str:
        u, d = (up[t] or 0) > 0, (down[t] or 0) < 0
        return "both" if u and d else "up_only" if u else "down_only" if d else "none"

    classes = {t: cls(t) for t in quarters}
    print("\nShare of quarters by activation (375 > 0 is up, 376 < 0 is down)")
    for name in ("both", "up_only", "down_only", "none"):
        count = sum(c == name for c in classes.values())
        print(f"  {name:10s} {count:3d} of {n} ({count / n:.0%})")

    hours = {t.replace(minute=0) for t in quarters}
    values_per_hour = {
        h: {direction[t] for t in quarters if t.replace(minute=0) == h} for h in hours
    }
    changing = sum(len(v) > 1 for v in values_per_hour.values())
    print(f"\nDominating direction 369 changes within the hour in {changing} of {len(hours)} hours")

    print("\nDominating direction 369 against single-direction quarters")
    single = {"up_only": 1, "down_only": -1}
    checked = [t for t in quarters if classes[t] in single]
    clash = [t for t in checked if direction[t] != single[classes[t]]]
    print(f"  {len(checked) - len(clash)} of {len(checked)} agree; disagreeing quarters:")
    for t in clash:
        print(f"    {t:%Y-%m-%dT%H:%MZ} up={up[t]} down={down[t]} dominating={direction[t]}")
    both = [t for t in quarters if classes[t] == "both"]
    print(
        f"  quarters with both peaks: dominating up {sum(direction[t] == 1 for t in both)}, "
        f"down {sum(direction[t] == -1 for t in both)}, none {sum(direction[t] == 0 for t in both)}"
    )

    print("\nImbalance price 319 by class (EUR/MWh; descriptive, one window, not causal)")
    for name in ("up_only", "down_only", "both"):
        xs = [price[t] for t in quarters if classes[t] == name and price.get(t) is not None]
        if len(xs) >= 2:
            q1, _, q3 = statistics.quantiles(xs, n=4)
            med = statistics.median(xs)
            print(f"  {name:10s} n={len(xs):3d} median={med:7.2f} IQR=[{q1:.2f}, {q3:.2f}]")

    print("\n375 and 376 are the maximum power of each quarter hour in MW. They are not")
    print("converted to MWh and the two directions are not netted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="required: enables source requests")
    if parser.parse_args().live:
        main()
    else:
        parser.print_help()
