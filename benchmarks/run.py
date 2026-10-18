"""Replayed-fixture benchmark harness for nordic-balancing.

Each measurement runs in a fresh subprocess (``--child``). A child loads the
fixture set outside every timer, then runs the workload's real client call
through ``ReplayTransport`` once, with per-stage timers wrapped onto the real
code path (transport, ZIP read, decode, parse+validate, record construction
including the ``raw`` deep copy, merge ``dataclasses.replace``), followed by a
standalone ``to_frame`` timing. Wall, CPU (user+sys) and peak RSS are recorded
per child; the parent aggregates median/p95 over N children after a warmup.

Stage timers are *inclusive*: ``parse_validate`` contains ``decode`` for
ENTSO-E and ``construction`` for eSett/EDS imbalance; ``residual_other`` is
what e2e wall does not attribute (dedupe, merges, expansion, sorting, EDS
capacity's inline parse). Live provider latency is reported only from the
recorded ``elapsed_s`` values, never mixed with replay timings.

Usage:
  python benchmarks/run.py --sets entsoe-imb-SE3-day-2025-10-26 --tag baseline
  python benchmarks/run.py --child <set> --fixtures-root <dir>

Requires the ``benchmarks`` dependency group (psutil); to_frame needs pandas.
"""

import argparse
import contextlib
import functools
import gc
import importlib.metadata
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import httpx
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures
import workloads

FIXTURES_ROOT = Path(os.environ.get("NB_FIXTURES", r"C:\Docker\nb-perf-fixtures"))
RESULTS_DIR = Path(__file__).resolve().parent / "results"


class Timer:
    """Thread-safe accumulating wall-time counter.

    Wrapped calls are only ever made from the serial instrumented pass
    (``max_workers=1``), so per-call wall time is additive CPU attribution.
    """

    def __init__(self) -> None:
        self.total = 0.0
        self.calls = 0
        self._lock = threading.Lock()

    def timed(self, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            began = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                with self._lock:
                    self.total += time.perf_counter() - began
                    self.calls += 1

        return wrapper

    def wrap_class(self, cls):
        @functools.wraps(cls)
        def factory(*args, **kwargs):
            began = time.perf_counter()
            try:
                return cls(*args, **kwargs)
            finally:
                with self._lock:
                    self.total += time.perf_counter() - began
                    self.calls += 1

        return factory


class _PeakRss:
    def __init__(self, process: psutil.Process) -> None:
        self._process = process
        self._samples: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(0.01):
            self._samples.append(self._process.memory_info().rss)

    def peak(self) -> int:
        info = self._process.memory_info()
        candidates = [info.rss, max(self._samples, default=0)]
        peak_wset = getattr(info, "peak_wset", None)  # Windows kernel-tracked peak
        if peak_wset:
            candidates.append(int(peak_wset))
        try:
            import resource
        except ImportError:
            pass
        else:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            candidates.append(peak if sys.platform == "darwin" else peak * 1024)
        return max(candidates)


def _stage_setup(name: str, http: httpx.Client, timers: dict[str, Timer]) -> list:
    """Wrap the stage boundaries of ``name``'s provider; returns restore thunks."""
    workload = workloads.WORKLOADS[name]
    timers.update(
        {
            key: Timer()
            for key in (
                "transport",
                "zip_read",
                "decode",
                "parse_validate",
                "construction",
                "dataclass_replace",
                "to_frame",
            )
        }
    )
    restores: list = []

    def patch(obj, attr: str, value) -> None:
        restores.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    patch(http, "get", timers["transport"].timed(http.get))
    if workload.provider == "entsoe":
        from nordic_balancing.sources import entsoe

        patch(entsoe, "_members", timers["zip_read"].timed(entsoe._members))
        patch(
            entsoe,
            "ElementTree",
            types.SimpleNamespace(
                fromstring=timers["decode"].timed(entsoe.ElementTree.fromstring),
                ParseError=entsoe.ElementTree.ParseError,
                Element=entsoe.ElementTree.Element,
            ),
        )
        patch(entsoe, "_points", timers["parse_validate"].timed(entsoe._points))
        patch(
            entsoe,
            "ImbalancePrice",
            timers["construction"].wrap_class(entsoe.ImbalancePrice),
        )
        patch(
            entsoe,
            "ReserveCapacity",
            timers["construction"].wrap_class(entsoe.ReserveCapacity),
        )
        # dataclasses.replace builds records via obj.__class__, bypassing the
        # module-global patches above; timed separately (merges and _Point
        # replacements share this counter).
        patch(entsoe, "replace", timers["dataclass_replace"].timed(entsoe.replace))
    else:
        # Both JSON sources decode through response.json() inside get_json.
        patch(httpx.Response, "json", timers["decode"].timed(httpx.Response.json))
        if workload.provider == "esett":
            from nordic_balancing.sources import esett as module
        else:
            from nordic_balancing.sources import energidataservice as module
        if hasattr(module, "_parse_imbalance"):
            patch(
                module,
                "_parse_imbalance",
                timers["parse_validate"].timed(module._parse_imbalance),
            )
        for record_class in ("ImbalancePrice", "ReserveCapacity"):
            if hasattr(module, record_class):
                patch(
                    module,
                    record_class,
                    timers["construction"].wrap_class(getattr(module, record_class)),
                )
        if workload.provider == "esett":
            # Hourly records expand to quarters through dataclasses.replace;
            # each replacement re-runs __post_init__ and the raw deep copy.
            patch(module, "replace", timers["construction"].timed(module.replace))
    return restores


def _undo(restores: list) -> None:
    for obj, attr, value in reversed(restores):
        setattr(obj, attr, value)


def _child(set_name: str, fixtures_root: Path) -> dict:
    from nordic_balancing import to_frame

    workload = workloads.WORKLOADS[set_name]
    set_dir = fixtures_root / set_name
    info = fixtures.load_set(set_dir)  # fixture loading is outside every timer
    timers: dict[str, Timer] = {}
    process = psutil.Process()
    result: dict = {"set": set_name, "ok": False}
    with _PeakRss(process) as sampler:
        # Pass 1: instrumented, serial — stage wall times add up without
        # thread-contention noise. e2e wall below is the real default.
        http = fixtures.replay_client(set_dir)
        restores = _stage_setup(set_name, http, timers)
        serial_error = None
        serial_began = time.perf_counter()
        serial_records: list = []
        try:
            serial_records = workload.run(http, api_key="replay", max_workers=1)
        except Exception as exc:
            serial_error = f"{type(exc).__name__}: {exc}"
        serial_wall_s = time.perf_counter() - serial_began
        _undo(restores)
        serial_count = len(serial_records)
        del serial_records  # don't let pass-1 objects tax pass-2's GC
        gc.collect()
        to_frame([])  # warm the lazy pandas import outside the timed call
        # Pass 2: unpatched, default concurrency — the measured e2e.
        http = fixtures.replay_client(set_dir)
        cpu0 = process.cpu_times()
        began = time.perf_counter()
        error = serial_error
        records = []
        try:
            records = workload.run(http, api_key="replay")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        wall_s = time.perf_counter() - began
        cpu = process.cpu_times()
        try:
            frame = timers["to_frame"].timed(to_frame)(records) if records else None
        except Exception as exc:
            frame = None
            result["to_frame_error"] = f"{type(exc).__name__}: {exc}"
        peak_rss = sampler.peak()
    top_level = {
        "entsoe": ["transport", "zip_read", "parse_validate", "construction", "dataclass_replace"],
        "esett": ["transport", "decode", "parse_validate"],
        "energidataservice": ["transport", "decode", "parse_validate", "construction"],
    }[workload.provider]
    stages = {key: timers[key].total for key in timers}
    stage_calls = {key: timers[key].calls for key in timers}
    result.update(
        {
            "ok": error is None,
            "error": error,
            "serial_wall_s": serial_wall_s,
            "serial_records": serial_count,
            "records": len(records),
            "frame_rows": len(frame) if frame is not None else 0,
            "requests": info["requests"],
            "response_bytes": info["total_bytes"],
            "live_elapsed_s": info["total_elapsed_s"],
            "wall_s": wall_s,
            "cpu_s": (cpu.user + cpu.system) - (cpu0.user + cpu0.system),
            "peak_rss_bytes": peak_rss,
            "stages_s": stages,
            "stage_calls": stage_calls,
            "residual_other_s": serial_wall_s - sum(stages[key] for key in top_level),
        }
    )
    return result


def _environment() -> dict:
    import nordic_balancing

    root = Path(nordic_balancing.__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
    except OSError:  # the slim benchmark image has no git
        commit = os.environ.get("NB_GIT_COMMIT", "unavailable")
    cgroups: dict = {}
    for name, path in (
        ("cpu.max", "/sys/fs/cgroup/cpu.max"),
        ("memory.max", "/sys/fs/cgroup/memory.max"),
    ):
        with contextlib.suppress(OSError):
            cgroups[name] = Path(path).read_text().strip()
    versions = {}
    for package in ("nordic-balancing", "httpx", "pandas", "psutil"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "host": {
            "platform": platform.platform(),
            "cpu": os.environ.get("PROCESSOR_IDENTIFIER", platform.processor()),
            "logical_cpus": psutil.cpu_count(logical=True),
            "physical_cpus": psutil.cpu_count(logical=False),
            "ram_bytes": psutil.virtual_memory().total,
        },
        "python": sys.version.split()[0],
        "packages": versions,
        "git_commit": commit,
        "in_container": Path("/.dockerenv").exists() or Path("/run/.containerenv").exists(),
        "cgroup_limits": cgroups or "none (not in a cgroup-limited container)",
        "docker_image": os.environ.get("NB_DOCKER_IMAGE"),
    }


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def _run_set(
    name: str, repeats: int, warmup: int, fixtures_root: Path, results_dir: Path, tag: str
) -> dict:
    script = str(Path(__file__).resolve())
    samples: list[dict] = []
    for index in range(warmup + repeats):
        completed = subprocess.run(
            [sys.executable, script, "--child", name, "--fixtures-root", str(fixtures_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"child for {name} failed:\n{completed.stderr}")
        sample = json.loads(completed.stdout.strip().splitlines()[-1])
        if index >= warmup:
            samples.append(sample)
        print(
            f"{name} {'warmup' if index < warmup else f'run {index - warmup + 1}/{repeats}'}: "
            f"wall={sample['wall_s']:.2f}s records={sample['records']} "
            f"ok={sample['ok']}",
            flush=True,
        )
    metrics = ["wall_s", "serial_wall_s", "cpu_s", "peak_rss_bytes"]
    stage_names = sorted({key for s in samples for key in s["stages_s"]} | {"residual_other_s"})
    summary = {
        "set": name,
        "tag": tag,
        "n": len(samples),
        "records": samples[0]["records"],
        "requests": samples[0]["requests"],
        "response_bytes": samples[0]["response_bytes"],
        "live_elapsed_s": samples[0]["live_elapsed_s"],
        "environment": _environment(),
        "samples": samples,
        "median": {},
        "p95": {},
    }
    for key in metrics + stage_names:
        flat = []
        for s in samples:
            value = s.get(key) if key in metrics else s["stages_s"].get(key)
            if key == "residual_other_s":
                value = s["residual_other_s"]
            if value is not None:
                flat.append(value)
        summary["median"][key] = statistics.median(flat) if flat else None
        summary["p95"][key] = _p95(flat)
    summary["records_per_s"] = (
        summary["records"] / summary["median"]["wall_s"] if summary["median"]["wall_s"] else None
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"{tag}__{name}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return summary


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", metavar="SET", help="run one measurement of SET and print JSON")
    parser.add_argument("--sets", help="comma-separated set names (default: all recorded)")
    parser.add_argument("--tag", default="run", help="label written into results files")
    parser.add_argument("--repeats", type=int, default=0)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--fixtures-root", type=Path, default=FIXTURES_ROOT)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)
    if args.child:
        print(json.dumps(_child(args.child, args.fixtures_root)))
        return 0
    if args.sets:
        names = args.sets.split(",")
    else:
        names = [
            path.name
            for path in sorted(args.fixtures_root.iterdir())
            if path.is_dir() and (path / "set.json").exists()
        ]
    unknown = sorted(set(names) - set(workloads.WORKLOADS))
    if unknown:
        parser.error(f"unknown sets: {unknown}")
    for name in names:
        repeats = args.repeats
        if not repeats:
            # Probe one child: sub-second workloads get N>=7, year workloads N>=3.
            probe = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    name,
                    "--fixtures-root",
                    str(args.fixtures_root),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if probe.returncode != 0:
                raise RuntimeError(f"probe for {name} failed:\n{probe.stderr}")
            wall = json.loads(probe.stdout.strip().splitlines()[-1])["wall_s"]
            repeats = 7 if wall < 2.0 else 3
            print(f"{name}: probe wall={wall:.2f}s -> repeats={repeats}", flush=True)
        _run_set(name, repeats, args.warmup, args.fixtures_root, args.results_dir, args.tag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
