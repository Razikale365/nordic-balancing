"""Correctness-first, strictly offline comparison. Run with ``python -m benchmarks.compare``."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import psutil

from benchmarks.areas import ENTSOE_AREAS, ESETT_AREAS
from benchmarks.replay import FrozenSet

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = Path("C:/Docker/nb-perf-fixtures")
REQUIRED = (
    "entsoe-imb-SE3-day-2025-10-26",
    "entsoe-imb-SE3-localday-2025-10-26",
    "entsoe-imb-SE3-month-2025-10",
    "entsoe-imb-SE3-year-2025",
    "entsoe-imb-all-year-2025",
    "entsoe-imb-SE3-year-2022",
    "entsoe-cap-FCRN-NO-year-2025",
    "esett-imb-SE3-year-2025",
    "esett-imb-all-year-2025",
    "eds-imb-DK-year-2025",
    "eds-cap-FCRN-year-2025",
    "eds-cap-aFRR-month-2025-10",
)


def no_network(*args, **kwargs):
    raise RuntimeError("Network forbidden in frozen comparison")


def parameters(frozen):
    """Use actual recorded query instants, never infer a 25-hour window from its label."""
    queries = [parse_qs(urlsplit(key.split(" ", 1)[1]).query) for key in frozen.entries]
    provider = frozen.metadata["provider"]
    if provider == "entsoe":

        def parse(s):
            return datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=UTC)

        start = min(parse(q["periodStart"][0]) for q in queries)
        end = max(parse(q["periodEnd"][0]) for q in queries)
        codes = {q["controlArea_Domain"][0] for q in queries}
        zones = [zone for zone, code in ENTSOE_AREAS.items() if code in codes]
    elif provider == "esett":
        start = min(datetime.fromisoformat(q["start"][0]) for q in queries)
        end = max(datetime.fromisoformat(q["end"][0]) for q in queries)
        codes = {code for q in queries for code in q["mba"]}
        zones = [zone for zone, code in ESETT_AREAS.items() if code in codes]
    else:
        raise ValueError(f"No comparable client configured for {provider}")
    if not zones:
        raise ValueError("No supported zones in fixture")
    return start, end, zones


def operation(frozen, mode, stack):
    start, end, zones = parameters(frozen)
    capacity = "-cap-" in frozen.metadata["set"]
    if mode.startswith("nb-"):
        from nordic_balancing import (
            BiddingZone,
            EntsoeClient,
            ESettClient,
            ReserveProduct,
            to_frame,
        )

        zones = [BiddingZone(zone) for zone in zones]
        http = stack.enter_context(frozen.httpx())
        if frozen.metadata["provider"] == "entsoe":
            client = EntsoeClient(
                api_key="offline-placeholder", http=http, max_workers=1, max_retries=0
            )
        else:
            client = ESettClient(http=http, max_retries=0)
        if capacity:

            def call():
                return client.capacity_market(ReserveProduct.FCR_N, start, end, zones=zones)
        else:

            def call():
                return client.imbalance_prices(start, end, zones=zones)

        if mode == "nb-frame":

            def with_frame():
                records = call()
                frame_start = time.perf_counter()
                frame = to_frame(records)
                return records, frame, time.perf_counter() - frame_start

            return with_frame
        return call
    if mode == "esett-py":
        from esett import Client as EsettPyClient

        # Public common chunk configuration; both clients use 31-day requests.
        client = EsettPyClient(max_window=timedelta(days=31), retries=0)
        stack.enter_context(patch("urllib.request.urlopen", frozen.urlopen))
        return lambda: client.prices.values(
            start=start, end=end, mba=[ESETT_AREAS[z] for z in zones]
        )
    import pandas as pd
    from entsoe import EntsoePandasClient

    session = stack.enter_context(frozen.requests())
    client = EntsoePandasClient(
        api_key="offline-placeholder", session=session, retry_count=1, retry_delay=0
    )

    def query():
        frames = []
        for zone in zones:
            kwargs = dict(
                country_code=ENTSOE_AREAS[zone], start=pd.Timestamp(start), end=pd.Timestamp(end)
            )
            if capacity:
                frame = client.query_contracted_reserve_prices_procured_capacity(
                    **kwargs, process_type="A52", type_marketagreement_type="A01"
                )
            else:
                frame = client.query_imbalance_prices(**kwargs)
            frames.append((zone, frame))
        return frames

    return query


def number(value):
    if value is None:
        return None
    value = float(value)
    if math.isnan(value):
        return None
    if not math.isfinite(value):
        raise ValueError("Nonfinite price")
    return value


def canonical(output, mode, capacity=False):
    """Keep native frequency/order. Never fill missing intervals or collapse duplicates."""
    if mode == "nb-frame":
        output = output[0]
    rows = []
    if mode.startswith("nb-"):
        for record in output:
            if capacity:
                values = {"price": record.price_eur_per_mw, "quantity": record.volume_mw}
            else:
                values = {"price": record.imbalance_price_eur}
                if record.source == "entsoe":
                    names = {"A04": "Long", "A05": "Short", None: "Price for Consumption"}
                    values = {
                        names.get(c["category"], c["category"]): c["amount"]
                        for c in record.raw["categories"]
                    }
            rows.append(
                {
                    "zone": record.zone.value,
                    "start": record.start.isoformat(),
                    "values": {k: number(v) for k, v in values.items()},
                    "resolution_s": record.resolution.total_seconds(),
                }
            )
    elif mode == "entsoe-py":
        for zone, frame in output:
            for timestamp, row in frame.iterrows():
                rows.append(
                    {
                        "zone": zone,
                        "start": timestamp.tz_convert("UTC").isoformat(),
                        "values": {str(k): number(v) for k, v in row.items()},
                    }
                )
    else:
        reverse = {v: k for k, v in ESETT_AREAS.items()}
        for row in output:
            rows.append(
                {
                    "zone": reverse.get(row["mba"], row["mba"]),
                    "start": datetime.fromisoformat(row["timestampUTC"])
                    .astimezone(UTC)
                    .isoformat(),
                    "values": {"price": number(row["imblSalesPrice"])},
                }
            )
    return rows


def native_output_info(output, mode):
    if mode == "nb-frame":
        return {
            "type": "DataFrame",
            "columns": list(output[1].columns),
            "records": len(output[1]),
            "raw_preserved": False,
            "raw_in_intermediate_records": True,
        }
    if mode == "nb-records":
        return {
            "type": "dataclass list",
            "raw_preserved": True,
            "raw_keys": sorted({k for row in output for k in row.raw}),
            "direction": "typed for eSett/capacity; A85 flow direction retained raw only",
        }
    if mode == "entsoe-py":
        return {
            "type": "DataFrame per zone",
            "raw_preserved": False,
            "columns": sorted({str(k) for _, frame in output for k in frame.columns}),
            "timezones": sorted({str(frame.index.tz) for _, frame in output}),
        }
    return {
        "type": "list[dict]",
        "raw_preserved": True,
        "columns": sorted({k for row in output for k in row}),
    }


def audit(rows, start, end, zones):
    zones = [str(zone) for zone in zones]
    keys = [(r["zone"], r["start"]) for r in rows]
    counts = Counter(keys)
    expected = {
        (zone, stamp.isoformat())
        for zone in zones
        for stamp in (
            start + i * timedelta(minutes=15)
            for i in range(math.ceil((end - start) / timedelta(minutes=15)))
        )
    }
    actual = set(keys)
    # Order checked per zone; one library returns zone-major and the other time-major.
    reordered = sum(
        any(a > b for a, b in itertools.pairwise(stamps))
        for zone in zones
        if (stamps := [r["start"] for r in rows if r["zone"] == zone])
    )
    times = [r["start"] for r in rows]
    per_zone = {}
    for zone in zones:
        subset = [r for r in rows if r["zone"] == zone]
        zone_keys = {key for key in actual if key[0] == zone}
        missing = sorted(key[1] for key in expected - actual if key[0] == zone)
        per_zone[zone] = {
            "records": len(subset),
            "unique_intervals": len(zone_keys),
            "first_utc": min((r["start"] for r in subset), default=None),
            "last_utc": max((r["start"] for r in subset), default=None),
            "missing_quarters": len(missing),
            "first_missing_quarters": missing[:8],
            "null_values": sum(v is None for r in subset for v in r["values"].values()),
        }
    resolution_counts = Counter(r["resolution_s"] for r in rows if "resolution_s" in r)
    return {
        "records": len(rows),
        "unique_intervals": len(actual),
        "first_utc": min(times, default=None),
        "last_utc": max(times, default=None),
        "expected_quarters": len(expected),
        "missing_quarters": len(expected - actual),
        "unexpected_intervals": len(actual - expected),
        "duplicate_intervals": sum(n - 1 for n in counts.values()),
        "reordered_zones": reordered,
        "null_values": sum(v is None for r in rows for v in r["values"].values()),
        "source_resolutions_s": sorted({r["resolution_s"] for r in rows if "resolution_s" in r}),
        "values_columns": sorted({k for r in rows for k in r["values"]}),
        "all_utc": all(datetime.fromisoformat(t).utcoffset() == timedelta(0) for t in times),
        "per_zone": per_zone,
        "source_resolution_record_counts": dict(resolution_counts),
    }


def differences(left, right):
    # Counter preserves duplicate evidence; no winner may be assigned if it differs.
    lkeys = Counter((r["zone"], r["start"]) for r in left)
    rkeys = Counter((r["zone"], r["start"]) for r in right)
    rmap = {(r["zone"], r["start"]): r for r in right}
    delta = []
    null_mismatch = 0
    column_mismatch = 0
    for row in left:
        other = rmap.get((row["zone"], row["start"]))
        if other is None:
            continue
        if row["values"].keys() != other["values"].keys():
            column_mismatch += 1
        for key in row["values"].keys() & other["values"].keys():
            a, b = row["values"][key], other["values"][key]
            if (a is None) != (b is None):
                null_mismatch += 1
            elif a is not None:
                delta.append(abs(a - b))
    return {
        "nb_only_intervals": sum((lkeys - rkeys).values()),
        "alternative_only_intervals": sum((rkeys - lkeys).values()),
        "null_mismatches": null_mismatch,
        "column_mismatches": column_mismatch,
        "compared_values": len(delta),
        "max_abs_price_difference": max(delta, default=None),
    }


def worker(directory, mode):
    frozen = FrozenSet(directory)
    with ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "connect", no_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", no_network))
        stack.enter_context(patch("socket.create_connection", no_network))
        call = operation(frozen, mode, stack)
        try:
            warmup = call()
            del warmup
            gc.collect()
            frozen.used.clear()
            cpu = time.process_time()
            wall = time.perf_counter()
            output = call()
            elapsed = time.perf_counter() - wall
            cpu_s = time.process_time() - cpu
            memory = psutil.Process().memory_info()
            if not hasattr(memory, "peak_wset"):
                raise RuntimeError(
                    "Peak RSS measurement currently requires Windows psutil peak_wset"
                )
            rss = memory.peak_wset
            rows = canonical(output, mode, "-cap-" in frozen.metadata["set"])
            start, end, zones = parameters(frozen)
            result = {
                "status": "PASS",
                "wall_s": elapsed,
                "cpu_s": cpu_s,
                "peak_rss_bytes": rss,
                "records_s": len(rows) / elapsed,
                "native_output": native_output_info(output, mode),
                "frame_s": output[2] if mode == "nb-frame" else None,
                "audit": audit(rows, start, end, zones),
                "rows": rows,
            }
        except Exception as exc:
            result = {
                "status": "NOT_COMPARABLE" if frozen.misses else "ERROR",
                "error_type": type(exc).__name__,
                "error": str(exc)[:2000],
            }
    return {
        **result,
        "requests_used": frozen.used,
        "missing_keys": frozen.misses,
        "request_mappings": [],
        "fixture_provenance": frozen.provenance(),
    }


def summarize(samples):
    output = {"samples": len(samples)}
    for metric in ("wall_s", "cpu_s", "peak_rss_bytes", "records_s", "frame_s"):
        values = [s[metric] for s in samples if s.get(metric) is not None]
        if values:
            output[metric] = {
                "median": statistics.median(values),
                "p95": statistics.quantiles(values, n=100, method="inclusive")[94]
                if len(values) >= 20
                else None,
                "min": min(values),
                "max": max(values),
            }
    return output


def run_child(directory, mode, scratch):
    scratch = scratch / directory.name
    scratch.mkdir(parents=True, exist_ok=True)
    target = scratch / f"{mode}.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.compare",
            "--worker",
            str(directory),
            "--mode",
            mode,
            "--output",
            str(target),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if completed.returncode:
        return {"status": "ERROR", "stderr": completed.stderr[-2000:]}
    return json.loads(target.read_text(encoding="utf-8"))


def compare_set(directory, scratch, repeats=None):
    frozen = FrozenSet(directory)
    provider = frozen.metadata["provider"]
    if provider not in ("entsoe", "esett"):
        return {"status": "NOT_COMPARABLE", "reason": "No equivalent selected-client endpoint"}
    alternative = "entsoe-py" if provider == "entsoe" else "esett-py"
    modes = ("nb-records", "nb-frame", alternative)
    results = {mode: run_child(directory, mode, scratch) for mode in modes}
    diff = None
    if all(r["status"] == "PASS" for r in results.values()):
        diff = differences(results["nb-records"]["rows"], results[alternative]["rows"])
    # Record correctness before any performance repetitions. Diffs require review, no ranking.
    count = repeats or (3 if "year" in directory.name else 7)
    if count < (3 if "year" in directory.name else 7):
        raise ValueError("Too few repeats for packet gate")
    performance = {}
    for mode, first in results.items():
        if first["status"] != "PASS":
            continue
        samples = [first]
        for _ in range(count - 1):
            sample = run_child(directory, mode, scratch)
            if sample["status"] != "PASS":
                raise RuntimeError("Replay became nondeterministic")
            if sample["rows"] != first["rows"]:
                raise RuntimeError("Output became nondeterministic")
            samples.append(sample)
        performance[mode] = {
            "summary": summarize(samples),
            "samples": [
                {k: s.get(k) for k in ("wall_s", "cpu_s", "peak_rss_bytes", "records_s", "frame_s")}
                for s in samples
            ],
        }
    for result in results.values():
        result.pop("rows", None)
    return {
        "status": "MEASURED_REQUIRES_CLASSIFICATION" if diff else "NOT_COMPARABLE",
        "fixture_metadata": frozen.metadata,
        "correctness": results,
        "differences": diff,
        "performance": performance,
        "winner": None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "benchmarks/results/compare-real.json"
    )
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--mode", choices=("nb-records", "nb-frame", "entsoe-py", "esett-py"))
    parser.add_argument("--repeats", type=int)
    args = parser.parse_args()
    if args.worker:
        payload = worker(args.worker, args.mode)
    else:
        ready = (args.fixtures / "READY").is_file()
        scratch = ROOT / ".jpcp/perf/runs"
        scratch.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "MEASURED" if ready else "BLOCKED_FIXTURES_NOT_READY",
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "processor": platform.processor(),
                "logical_cpus": os.cpu_count(),
                "host_memory_bytes": psutil.virtual_memory().total,
                "cpu_affinity": psutil.Process().cpu_affinity(),
                "comparison_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "git_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "versions": {
                    p: importlib.metadata.version(p)
                    for p in ("entsoe-py", "esett-py", "httpx", "pandas", "psutil")
                },
            },
            "method": {
                "transport": "frozen replay; client-side processing only",
                "warmups_per_subprocess": 1,
                "measurements_per_subprocess": 1,
                "peak_rss_scope": "process lifetime including warmup, imports and loaded fixtures",
                "p95_min_samples": 20,
                "docker": "NOT_EXECUTED",
                "native_codex_children": 0,
            },
            "sets": {},
        }
        for name in REQUIRED:
            directory = args.fixtures / name
            if not ready:
                payload["sets"][name] = {
                    "status": "NOT_EXECUTED",
                    "reason": "READY absent",
                    "manifest_present": (directory / "manifest.jsonl").is_file(),
                }
            elif not (directory / "manifest.jsonl").is_file():
                payload["sets"][name] = {
                    "status": "BLOCKED",
                    "reason": "Manifest missing after READY",
                }
            else:
                print(f"START {name}", flush=True)
                payload["sets"][name] = compare_set(directory, scratch, args.repeats)
                print(f"DONE {name}: {payload['sets'][name]['status']}", flush=True)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8"
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
