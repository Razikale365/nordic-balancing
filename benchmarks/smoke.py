"""Exercise the harness with handmade inputs, never market performance evidence."""

import json
from pathlib import Path

from benchmarks.compare import ROOT, compare_set, differences, worker
from benchmarks.tests.test_compare import END, START, document, make_set, period


def main():
    scratch = ROOT / ".jpcp/perf/smoke-runs"
    scratch.mkdir(parents=True, exist_ok=True)
    directory = ROOT / "benchmarks/.tmp/handmade-day"
    make_set(directory, document(period(START, END, 100)))
    result = {
        "kind": "HANDMADE_FUNCTIONAL_SMOKE_NOT_MARKET_EVIDENCE",
        "dst_25_hour": compare_set(directory, scratch, repeats=7),
        "adversarial": {},
    }
    for name, kwargs in [
        ("duplicate", {"positions": [1, 1, 2, 3, 4]}),
        ("missing-category", {"category": False}),
        ("missing-price", {"missing_price": True}),
    ]:
        from datetime import timedelta

        end = START + timedelta(hours=1)
        path = directory.parent / name
        make_set(path, document(period(START, end, 4, **kwargs)), START, end)
        outputs = {mode: worker(path, mode) for mode in ("nb-records", "entsoe-py")}
        for output in outputs.values():
            output.pop("rows", None)
        result["adversarial"][name] = outputs
    from datetime import UTC, datetime, timedelta

    start = datetime(2022, 1, 1, tzinfo=UTC)
    split, end = start + timedelta(hours=1), start + timedelta(hours=2)
    path = directory.parent / "mixed-resolution"
    make_set(path, document(period(start, split, 1, "PT60M"), period(split, end, 4)), start, end)
    outputs = {mode: worker(path, mode) for mode in ("nb-records", "entsoe-py")}
    diff = differences(outputs["nb-records"]["rows"], outputs["entsoe-py"]["rows"])
    for output in outputs.values():
        output.pop("rows", None)
    result["mixed_resolution"] = {
        "outputs": outputs,
        "differences": diff,
        "classification": "expected domain difference: native hourly vs quarter-hour expansion",
    }
    Path("benchmarks/results/compare-smoke.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
