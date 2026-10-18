"""cProfile + tracemalloc on a replayed fixture workload.

Runs the workload's real client call through ``ReplayTransport`` once, in the
current process, and prints/serialises the profile. Fixture loading is done
before profiling starts.

  uv run --group benchmarks python benchmarks/profile.py --set entsoe-imb-SE3-year-2025
  uv run --group benchmarks python benchmarks/profile.py --set ... --tracemalloc --top 30

``--stats PATH`` writes the binary pstats for later ``pstats`` browsing.
"""

import argparse
import cProfile
import json
import pstats
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402
import workloads  # noqa: E402

FIXTURES_ROOT = Path(r"C:\Docker\nb-perf-fixtures")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", required=True, dest="set_name")
    parser.add_argument("--fixtures-root", type=Path, default=FIXTURES_ROOT)
    parser.add_argument("--tracemalloc", action="store_true")
    parser.add_argument("--serial", action="store_true", help="max_workers=1 for clean attribution")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--stats", type=Path, help="write binary pstats here")
    args = parser.parse_args(argv)

    workload = workloads.WORKLOADS[args.set_name]
    http = fixtures.replay_client(args.fixtures_root / args.set_name)
    kwargs = {"max_workers": 1} if args.serial else {}

    if args.tracemalloc:
        tracemalloc.start()
    profiler = cProfile.Profile()
    profiler.enable()
    records = workload.run(http, api_key="replay", **kwargs)
    profiler.disable()

    stats = pstats.Stats(profiler)
    if args.stats:
        stats.dump_stats(str(args.stats))
    stats.sort_stats("cumulative").print_stats(args.top)
    stats.sort_stats("tottime").print_stats(args.top)
    print(f"records: {len(records)}")

    if args.tracemalloc:
        current, peak = tracemalloc.get_traced_memory()
        snapshot = tracemalloc.take_snapshot()
        tracemalloc.stop()
        print(f"tracemalloc: current={current / 2**20:.1f} MiB peak={peak / 2**20:.1f} MiB")
        for entry in snapshot.statistics("lineno")[: args.top]:
            print(entry)
        alloc = {
            "current_bytes": current,
            "peak_bytes": peak,
            "top": [
                {"trace": str(entry.traceback[0]), "size": entry.size, "count": entry.count}
                for entry in snapshot.statistics("traceback")[: args.top]
            ],
        }
        out = Path("benchmarks/results") / f"tracemalloc__{args.set_name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(alloc, indent=2), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
