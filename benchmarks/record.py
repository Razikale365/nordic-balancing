"""Record the FIXTURES.md fixture sets, one named set per invocation.

Run on the v0.3.0 code in ``C:\\Docker\\nb-wt\\perf-base`` so the frozen
fixtures capture the baseline's view of each API (the FCR-N year set records
the live traffic even though v0.3.0's parser rejects the October fall-back
rows; that is noted in ``set.json``, not hidden):

  uv run --project C:/Docker/nb-wt/perf-base ^
      --env-file C:/Docker/nordic-balancing/.env ^
      python C:/Docker/nb-wt/perf/benchmarks/record.py <set> [<set> ...]
  uv run --project C:/Docker/nb-wt/perf-base ^
      python C:/Docker/nb-wt/perf/benchmarks/record.py --finalize

API keys are read from the environment and never printed. After each set the
recorded files are scanned for the key values and the run fails if any match.
Live requests are sequential (``max_workers=1`` and RecordTransport's lock)
and paced by ``Workload.min_interval_s``.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures
import workloads

FIXTURES_ROOT = Path(r"C:\Docker\nb-perf-fixtures")
SECRET_ENV_VARS = ("ENTSOE_API_KEY", "FINGRID_API_KEY")


def _library_commit() -> str:
    import nordic_balancing

    root = Path(nordic_balancing.__file__).resolve().parents[2]
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _secret_bytes() -> list[bytes]:
    secrets: list[bytes] = []
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            secrets.extend({value.encode(), quote(value).encode(), quote_plus(value).encode()})
    return secrets


def _scan_for_secrets(set_dir: Path, secrets: list[bytes]) -> None:
    for path in set_dir.iterdir():
        data = path.read_bytes()
        for secret in secrets:
            if secret in data:
                raise SystemExit(f"SECRET LEAK: an API key value appears in {path}")


def _record(name: str, root: Path, secrets: list[bytes]) -> None:
    workload = workloads.WORKLOADS[name]
    set_dir = root / name
    set_dir.mkdir(parents=True, exist_ok=True)
    transport = fixtures.RecordTransport(set_dir, min_interval_s=workload.min_interval_s)
    http = httpx.Client(transport=transport, timeout=120.0)
    began = time.perf_counter()
    records: int | None = None
    error: str | None = None
    try:
        records = len(workload.run(http, max_workers=1))
    except Exception as exc:  # recorded anyway: the exchanges are real provider data
        error = f"{type(exc).__name__}: {exc}"
    wall_s = time.perf_counter() - began
    http.close()
    manifest = fixtures.load_manifest(set_dir)
    notes = []
    if transport.duplicates:
        notes.append(
            f"{len(transport.duplicates)} repeated request(s); only the first "
            "response was kept per the fixture contract"
        )
    if error is not None:
        notes.append(f"the library call raised on the recorded data: {error}")
    set_json = {
        "set": name,
        "provider": workload.provider,
        "call": workload.call,
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "library_commit": _library_commit(),
        "requests": len(manifest),
        "total_bytes": sum(entry["bytes"] for entry in manifest),
        "total_elapsed_s": sum(entry["elapsed_s"] for entry in manifest),
        "wall_s": wall_s,
        "records_returned": records,
        "notes": notes,
    }
    if error is not None:
        set_json["error"] = error
    (set_dir / "set.json").write_text(json.dumps(set_json, indent=2) + "\n", encoding="utf-8")
    _scan_for_secrets(set_dir, secrets)
    statuses = {}
    for entry in manifest:
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
    print(
        f"{name}: requests={len(manifest)} bytes={set_json['total_bytes']} "
        f"records={records} wall={wall_s:.1f}s statuses={statuses} "
        f"duplicates={len(transport.duplicates)}" + (f" ERROR={error}" if error else "")
    )


def _finalize(root: Path) -> None:
    lines = ["# Recorded fixture sets", ""]
    missing = []
    for name in workloads.WORKLOADS:
        set_dir = root / name
        try:
            info = fixtures.load_set(set_dir)
            manifest = fixtures.load_manifest(set_dir)
        except (OSError, json.JSONDecodeError) as exc:
            missing.append((name, f"not recorded ({exc})"))
            continue
        size = sum(path.stat().st_size for path in set_dir.iterdir())
        status = (
            f"call raised ({info.get('error', 'unknown error')})"
            if info["records_returned"] is None
            else f"{info['records_returned']} records"
        )
        lines.append(
            f"- `{name}`: {len(manifest)} requests, {size} bytes on disk, {status}"
            + (f"; notes: {'; '.join(info['notes'])}" if info["notes"] else "")
        )
    for name, reason in missing:
        lines.append(f"- `{name}`: {reason}")
    (root / "READY").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if missing:
        raise SystemExit("some sets are missing; READY lists the reasons")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sets", nargs="*", help="set names to record, in order")
    parser.add_argument("--finalize", action="store_true", help="write READY from recorded sets")
    parser.add_argument("--fixtures-root", type=Path, default=FIXTURES_ROOT)
    parser.add_argument(
        "--between-sets-s",
        type=float,
        default=15.0,
        help="pause between consecutive sets in one invocation",
    )
    args = parser.parse_args(argv)
    if args.finalize:
        _finalize(args.fixtures_root)
        return 0
    unknown = sorted(set(args.sets) - set(workloads.WORKLOADS))
    if unknown:
        parser.error(f"unknown sets: {unknown}; choose from {sorted(workloads.WORKLOADS)}")
    if not args.sets:
        parser.error("name at least one set, or --finalize")
    secrets = _secret_bytes()
    if not secrets:
        print("warning: no API keys in the environment; keyed sources will fail", flush=True)
    for index, name in enumerate(args.sets):
        if index:
            time.sleep(args.between_sets_s)
        _record(name, args.fixtures_root, secrets)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
