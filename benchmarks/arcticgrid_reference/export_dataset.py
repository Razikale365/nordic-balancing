"""Export frozen imbalance prices as an immutable, versioned dataset for ArcticGrid.

Step 1 of the reference integration (see ``benchmarks/ARCTICGRID_REFERENCE.md``). It replays a
recorded fixture set through nordic-balancing's public client API (no network), and writes one
JSON document with the normalised quarter-hour prices, the library version, the source call and
a SHA-256 over the canonical points. Unpublished prices stay ``null``.

Run from the repository root:
    uv run --group benchmarks python benchmarks/arcticgrid_reference/export_dataset.py \
        --set entsoe-imb-SE3-year-2025 --out <dir>/se3-2025.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import nordic_balancing
from benchmarks.fixtures import load_set, replay_client
from benchmarks.workloads import WORKLOADS


def _iso(value: object) -> str:
    return value.isoformat().replace("+00:00", "Z")  # type: ignore[attr-defined]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--set", required=True, help="fixture set name, e.g. entsoe-imb-SE3-year-2025"
    )
    parser.add_argument("--fixtures-root", default=r"C:\Docker\nb-perf-fixtures")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    set_dir = Path(args.fixtures_root) / args.set
    workload = WORKLOADS[args.set]
    # Replay only: the token is a placeholder that the replay transport strips from request keys.
    records = workload.run(replay_client(set_dir), api_key="replay-no-network")
    points = [
        {
            "start": _iso(r.start),
            "end": _iso(r.end),
            "zone": r.zone.value,
            "price_eur_mwh": r.imbalance_price_eur,
            "resolution_s": int(r.resolution.total_seconds()),
        }
        for r in records
    ]
    canonical = json.dumps(points, sort_keys=True, separators=(",", ":")).encode()
    document = {
        "dataset": args.set,
        "library": {"name": "nordic-balancing", "version": nordic_balancing.__version__},
        "source": records[0].source if records else None,
        "call": workload.call,
        "recorded_at": load_set(set_dir).get("recorded_at"),
        "points_sha256": hashlib.sha256(canonical).hexdigest(),
        "points": points,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=1), encoding="utf-8")
    nulls = sum(1 for p in points if p["price_eur_mwh"] is None)
    print(
        f"{args.set}: {len(points)} points, {nulls} unpublished, "
        f"sha256={document['points_sha256'][:16]}"
    )


if __name__ == "__main__":
    main()
