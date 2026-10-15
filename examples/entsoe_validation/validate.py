"""Compare ENTSO-E A85 imbalance prices with eSett, interval by interval.

Two fixed, bounded comparisons, all 12 Nordic zones:

  samples    five UTC windows (60 zone-windows, 72 zone-days): 2022-01-15 (hourly
             era), 2023-05-21T12:00Z+24h (eSett's 60 -> 15 min switch),
             2025-03-18T12:00Z+24h (first genuine quarter-hour prices),
             2025-10-25T22:00Z+24h (DST end) and 2026-10-10..11 (two days).
             12 ENTSO-E requests and one eSett query per window.
  year-2025  2025-01-01T00:00Z to 2026-01-01T00:00Z: 12 ENTSO-E requests (one per
             zone, in parallel; about 100 s) and one paged eSett query.
  multi-chunk  DK1 and SE3 only, 2024-06-01T00:00Z to 2025-07-01T00:00Z (395 days):
             two ENTSO-E requests per zone, split at the 366-day chunk boundary
             2025-06-02T00:00Z. Also fetches 2025-05-31..06-04 in one request per
             zone and records whether it equals the multi-chunk result there.

Live requests run only with ``--live``, and ENTSO-E needs ``ENTSOE_API_KEY`` in the
environment. Without ``--live`` the script fetches nothing and exits. Importing it
never touches the network; ``tests/test_entsoe_validation.py`` tests the
classification offline.

Every (zone, interval start) in either source lands in exactly one category:

  missing_in_entsoe    eSett has the interval, ENTSO-E does not
  missing_in_esett     ENTSO-E has the interval, eSett does not
  both_unpublished     both have it with a null price
  entsoe_unpublished   ENTSO-E's price is null, eSett's is not
  esett_unpublished    eSett's price is null, ENTSO-E's is not
  exact                equal prices
  rounding             prices differ by at most the tolerance (0.01 EUR/MWh)
  zero_price           one side is exactly 0.0, the other differs by more
  material             any other difference above the tolerance

The categories add up to the number of keys in the union. Different
``resolution`` labels on an interval present in both are counted separately
(``resolution_label_differs``); that is an attribute, not a category, because
ENTSO-E and eSett label the same prices differently (README, contract C7).

Run: uv run python examples/entsoe_validation/validate.py samples --live
Writes results_<mode>.json next to this file unless ``--out`` is given.
"""

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TOLERANCE_EUR = 0.01
QUARTER = timedelta(minutes=15)
CATEGORIES = (
    "missing_in_entsoe",
    "missing_in_esett",
    "both_unpublished",
    "entsoe_unpublished",
    "esett_unpublished",
    "exact",
    "rounding",
    "zero_price",
    "material",
)
WINDOWS: dict[str, list[tuple[str, datetime, datetime]]] = {
    "samples": [
        ("hourly-2022-01-15", datetime(2022, 1, 15, tzinfo=UTC), datetime(2022, 1, 16, tzinfo=UTC)),
        (
            "switch-2023-05-21",
            datetime(2023, 5, 21, 12, tzinfo=UTC),
            datetime(2023, 5, 22, 12, tzinfo=UTC),
        ),
        (
            "quarter-hour-2025-03-18",
            datetime(2025, 3, 18, 12, tzinfo=UTC),
            datetime(2025, 3, 19, 12, tzinfo=UTC),
        ),
        (
            "dst-2025-10-26",
            datetime(2025, 10, 25, 22, tzinfo=UTC),
            datetime(2025, 10, 26, 22, tzinfo=UTC),
        ),
        (
            "recent-2026-10-10",
            datetime(2026, 10, 10, tzinfo=UTC),
            datetime(2026, 10, 12, tzinfo=UTC),
        ),
    ],
    "year-2025": [
        ("year-2025", datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ],
    "multi-chunk": [
        ("multi-chunk-2024-06", datetime(2024, 6, 1, tzinfo=UTC), datetime(2025, 7, 1, tzinfo=UTC)),
    ],
}
# Modes limited to some zones; the others use all 12.
ZONES: dict[str, tuple[str, ...]] = {"multi-chunk": ("DK1", "SE3")}
# A window around the chunk boundary, fetched again in a single request per zone.
SEAMS: dict[str, tuple[datetime, datetime]] = {
    "multi-chunk": (datetime(2025, 5, 31, tzinfo=UTC), datetime(2025, 6, 4, tzinfo=UTC)),
}

# (zone, interval start) -> (price in EUR/MWh or None, resolution in minutes)
Key = tuple[str, datetime]
Series = Mapping[Key, tuple[float | None, int]]


def classify_one(entsoe: float | None, esett: float | None, tolerance: float) -> str:
    """The category of one interval present in both sources."""
    if entsoe is None or esett is None:
        if entsoe is None and esett is None:
            return "both_unpublished"
        return "entsoe_unpublished" if entsoe is None else "esett_unpublished"
    diff = abs(entsoe - esett)
    if diff == 0:
        return "exact"
    # Cent-precision decimals: 17.53 - 17.52 exceeds 0.01 only through float noise.
    if diff <= tolerance or math.isclose(diff, tolerance):
        return "rounding"
    if entsoe == 0.0 or esett == 0.0:
        return "zero_price"
    return "material"


def classify(entsoe: Series, esett: Series, tolerance: float = TOLERANCE_EUR) -> dict[Key, str]:
    """Exactly one category per key in the union of both series."""
    result: dict[Key, str] = {}
    for key in entsoe.keys() | esett.keys():
        if key not in entsoe:
            result[key] = "missing_in_entsoe"
        elif key not in esett:
            result[key] = "missing_in_esett"
        else:
            result[key] = classify_one(entsoe[key][0], esett[key][0], tolerance)
    return result


def runs(starts: Iterable[datetime]) -> list[list[str]]:
    """Contiguous quarter-hour runs as [first start, last start] pairs."""
    result: list[list[datetime]] = []
    for start in sorted(starts):
        if result and start - result[-1][1] == QUARTER:
            result[-1][1] = start
        else:
            result.append([start, start])
    return [[first.isoformat(), last.isoformat()] for first, last in result]


def summarise(entsoe: Series, esett: Series, tolerance: float = TOLERANCE_EUR) -> dict[str, Any]:
    """Counts per category and zone, missing runs, and every other non-exact interval."""
    classes = classify(entsoe, esett, tolerance)
    totals = Counter(classes.values())
    zones = sorted({zone for zone, _ in classes})
    per_zone = {zone: {name: 0 for name in CATEGORIES} for zone in zones}
    for (zone, _), name in classes.items():
        per_zone[zone][name] += 1
    labels = sum(1 for key in entsoe.keys() & esett.keys() if entsoe[key][1] != esett[key][1])
    missing = {
        side: {
            zone: runs(start for (z, start), name in classes.items() if z == zone and name == side)
            for zone in zones
        }
        for side in ("missing_in_entsoe", "missing_in_esett")
    }
    details = [
        {
            "zone": zone,
            "start": start.isoformat(),
            "category": name,
            "entsoe": entsoe[(zone, start)][0],
            "esett": esett[(zone, start)][0],
        }
        for (zone, start), name in sorted(classes.items())
        if name not in ("exact", "missing_in_entsoe", "missing_in_esett")
    ]
    return {
        "keys": len(classes),
        "entsoe_rows": len(entsoe),
        "esett_rows": len(esett),
        "totals": {name: totals[name] for name in CATEGORIES},
        "resolution_label_differs": labels,
        "per_zone": per_zone,
        "missing_runs": {side: {z: r for z, r in by.items() if r} for side, by in missing.items()},
        "differences": details,
    }


def fetch(
    start: datetime, end: datetime, zones: tuple[str, ...] | None = None
) -> tuple[Series, Series]:
    """Both sources over [start, end) for ``zones`` (default all 12). Live requests."""
    from nordic_balancing import BiddingZone, EntsoeClient, ESettClient, ImbalancePrice

    selected = tuple(BiddingZone(z) for z in zones) if zones else tuple(BiddingZone)

    def series(prices: list[ImbalancePrice]) -> dict[Key, tuple[float | None, int]]:
        return {
            (p.zone.value, p.start): (
                p.imbalance_price_eur,
                int(p.resolution.total_seconds()) // 60,
            )
            for p in prices
        }

    with EntsoeClient() as entsoe, ESettClient() as esett:
        return series(entsoe.imbalance_prices(start, end, selected)), series(
            esett.imbalance_prices(start, end, selected)
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("mode", choices=sorted(WINDOWS))
    parser.add_argument("--live", action="store_true", help="required: call ENTSO-E and eSett")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("this script calls live APIs; pass --live to run it")
    out = args.out or Path(__file__).with_name(f"results_{args.mode}.json")
    windows = []
    for label, start, end in WINDOWS[args.mode]:
        began = time.perf_counter()
        zones = ZONES.get(args.mode)
        entsoe, esett = fetch(start, end, zones)
        summary = summarise(entsoe, esett)
        seconds = round(time.perf_counter() - began)
        print(label, f"{seconds} s", summary["totals"], flush=True)
        window = {"label": label, "start": start.isoformat(), "end": end.isoformat(), **summary}
        if args.mode in SEAMS:
            first, last = SEAMS[args.mode]
            single, _ = fetch(first, last, zones)
            chunked = {key: value for key, value in entsoe.items() if first <= key[1] < last}
            window["seam"] = {
                "start": first.isoformat(),
                "end": last.isoformat(),
                "multi_chunk_intervals": len(chunked),
                "single_request_intervals": len(single),
                "identical": chunked == single,
            }
            print("seam", window["seam"], flush=True)
        windows.append(window)
    totals = {name: sum(w["totals"][name] for w in windows) for name in CATEGORIES}
    result = {
        "mode": args.mode,
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tolerance_eur": TOLERANCE_EUR,
        "totals": totals,
        "windows": windows,
    }
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print("totals", totals)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
