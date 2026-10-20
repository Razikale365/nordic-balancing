"""Prove replayed outputs are identical between two versions of the library.

``dump`` runs every fixture set through the replay transport in a fresh
subprocess per set and writes a canonical JSONL stream of the returned
records (every field including ``raw``, in order). ``diff`` compares two dump
trees line by line and reports the first difference per set.

  python benchmarks/compare_outputs.py dump --tag before [--sets a,b]
  git checkout <candidate>
  python benchmarks/compare_outputs.py dump --tag after [--sets a,b]
  python benchmarks/compare_outputs.py diff before after

The dump for a set whose call raises contains the error instead of records;
two versions are identical only when the error matches too.
"""

import argparse
import dataclasses
import enum
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures
import workloads

FIXTURES_ROOT = fixtures.FIXTURES_ROOT
DUMPS_ROOT = Path(__file__).resolve().parent / "dumps"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, enum.Enum):
        return value.value
    return value


def _canonical(record: Any) -> str:
    row = {field.name: getattr(record, field.name) for field in dataclasses.fields(record)}
    return json.dumps(_jsonable(row), sort_keys=True)


def _child(set_name: str, fixtures_root: Path) -> dict:
    workload = workloads.WORKLOADS[set_name]
    http = fixtures.replay_client(fixtures_root / set_name)
    try:
        records = workload.run(http, api_key="replay")
    except Exception as exc:
        return {"set": set_name, "error": f"{type(exc).__name__}: {exc}", "lines": []}
    return {"set": set_name, "error": None, "lines": [_canonical(r) for r in records]}


def _dump(tag: str, names: list[str], fixtures_root: Path, dumps_root: Path) -> None:
    out_dir = dumps_root / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    for name in names:
        completed = subprocess.run(
            [sys.executable, script, "--child", name, "--fixtures-root", str(fixtures_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"child for {name} failed:\n{completed.stderr}")
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        digest = hashlib.sha256("\n".join(result["lines"]).encode()).hexdigest()
        header = {"set": name, "records": len(result["lines"]), "error": result["error"]}
        if result["error"] is None:
            header["sha256"] = digest
        path = out_dir / f"{name}.jsonl"
        path.write_text(
            json.dumps(header) + "\n" + "\n".join(result["lines"]) + "\n", encoding="utf-8"
        )
        print(
            f"{name}: {len(result['lines'])} records sha256={digest[:16]} error={result['error']}"
        )


def _diff(tag_a: str, tag_b: str, dumps_root: Path) -> int:
    dir_a, dir_b = dumps_root / tag_a, dumps_root / tag_b
    files_a = {p.name for p in dir_a.glob("*.jsonl")}
    files_b = {p.name for p in dir_b.glob("*.jsonl")}
    if files_a != files_b:
        print(
            f"dump sets differ: only in {tag_a}: {sorted(files_a - files_b)}; "
            f"only in {tag_b}: {sorted(files_b - files_a)}"
        )
        return 1
    failed = False
    for filename in sorted(files_a):
        lines_a = (dir_a / filename).read_text(encoding="utf-8").splitlines()
        lines_b = (dir_b / filename).read_text(encoding="utf-8").splitlines()
        if lines_a == lines_b:
            header = json.loads(lines_a[0])
            print(f"{filename}: identical ({header.get('records', '?')} records)")
            continue
        failed = True
        print(f"{filename}: DIFFERS")
        for index, (a, b) in enumerate(zip(lines_a, lines_b, strict=False)):
            if a != b:
                print(
                    f"  first difference at line {index + 1}:\n    A: {a[:400]}\n    B: {b[:400]}"
                )
                break
        if len(lines_a) != len(lines_b):
            print(f"  line counts differ: {len(lines_a)} vs {len(lines_b)}")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", metavar="SET", help=argparse.SUPPRESS)
    parser.add_argument("--fixtures-root", type=Path, default=FIXTURES_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--dumps-root", type=Path, default=DUMPS_ROOT)
    sub = parser.add_subparsers(dest="command")
    dump = sub.add_parser("dump", help="record canonical outputs into dumps/<tag>/")
    dump.add_argument("--tag", required=True)
    dump.add_argument("--sets", help="comma-separated names (default: all recorded)")
    diff = sub.add_parser("diff", help="compare two dumps")
    diff.add_argument("tag_a")
    diff.add_argument("tag_b")
    args = parser.parse_args(argv)
    if args.child:
        print(json.dumps(_child(args.child, args.fixtures_root)))
        return 0
    if args.command == "dump":
        if args.sets:
            names = args.sets.split(",")
        else:
            names = [
                p.name
                for p in sorted(args.fixtures_root.iterdir())
                if p.is_dir() and (p / "set.json").exists()
            ]
        unknown = sorted(set(names) - set(workloads.WORKLOADS))
        if unknown:
            parser.error(f"unknown sets: {unknown}")
        _dump(args.tag, names, args.fixtures_root, args.dumps_root)
        return 0
    if args.command == "diff":
        return _diff(args.tag_a, args.tag_b, args.dumps_root)
    parser.error("choose dump or diff")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
