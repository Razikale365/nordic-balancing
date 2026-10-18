"""Independent untimed inspection of frozen A85 XML publication coverage.

This is evidence for interval differences, not an alternative timed client. It
supports the two source resolutions and curve types present in this corpus and
fails on unknown shapes. It never fills across Period boundaries.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from benchmarks.areas import ENTSOE_AREAS
from benchmarks.compare import DEFAULT_FIXTURES, REQUIRED, ROOT, parameters
from benchmarks.replay import FrozenSet

NAMES = {"A04": "Long", "A05": "Short", None: "Price for Consumption"}


def publication(frozen):
    first, last, _ = parameters(frozen)
    native, quarters = {}, {}
    resolutions = Counter()
    period_ranges = []
    explicit_duplicates = 0
    for key, (entry, body) in frozen.entries.items():
        if entry["status"] != 200:
            raise ValueError("A85 oracle expects successful recorded responses")
        from urllib.parse import parse_qs, urlsplit

        code = parse_qs(urlsplit(key.split(" ", 1)[1]).query)["controlArea_Domain"][0]
        zone = next(zone for zone, area in ENTSOE_AREAS.items() if area == code)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            for member in archive.namelist():
                if not member.lower().endswith(".xml"):
                    continue
                root = ET.fromstring(archive.read(member))
                ns = {"x": root.tag.split("}")[0].lstrip("{")}
                for series in root.findall("x:TimeSeries", ns):
                    curve = series.findtext("x:curveType", namespaces=ns)
                    if curve not in ("A01", "A03"):
                        raise ValueError(f"Unexpected curve {curve}")
                    for period in series.findall("x:Period", ns):

                        def text(name, node=period, namespaces=ns):
                            return node.findtext("x:" + name, namespaces=namespaces)

                        start = datetime.fromisoformat(text("timeInterval/x:start")).astimezone(UTC)
                        end = datetime.fromisoformat(text("timeInterval/x:end")).astimezone(UTC)
                        resolution = text("resolution")
                        if resolution not in ("PT15M", "PT60M"):
                            raise ValueError(f"Unexpected resolution {resolution}")
                        step = timedelta(minutes=15 if resolution == "PT15M" else 60)
                        period_ranges.append(
                            {
                                "zone": zone,
                                "start": start.isoformat(),
                                "end": end.isoformat(),
                                "resolution": resolution,
                            }
                        )
                        points = {}
                        for point in period.findall("x:Point", ns):
                            position = int(point.findtext("x:position", namespaces=ns))
                            category = point.findtext("x:imbalance_Price.category", namespaces=ns)
                            amount = point.findtext("x:imbalance_Price.amount", namespaces=ns)
                            if category not in NAMES:
                                raise ValueError(f"Unknown category {category}")
                            if position in points:
                                explicit_duplicates += 1
                            points[position] = (
                                NAMES[category],
                                None if amount is None else float(amount),
                            )
                        carry = None
                        for offset in range(int((end - start) / step)):
                            value = points.get(offset + 1)
                            if value is not None:
                                carry = value
                            elif curve == "A03":
                                value = carry
                            if value is None:
                                continue
                            stamp = start + offset * step
                            name, amount = value
                            if first <= stamp < last:
                                row = native.setdefault((zone, stamp.isoformat()), {})
                                if name in row and row[name] != amount:
                                    raise ValueError("Conflicting source categories")
                                row[name] = amount
                                resolutions[(zone, resolution)] += 1
                            for quarter in range(int(step / timedelta(minutes=15))):
                                tick = stamp + quarter * timedelta(minutes=15)
                                if first <= tick < last:
                                    row = quarters.setdefault((zone, tick.isoformat()), {})
                                    if name in row and row[name] != amount:
                                        raise ValueError("Conflicting source quarters")
                                    row[name] = amount
    return (
        native,
        quarters,
        {
            "explicit_duplicate_positions": explicit_duplicates,
            "source_periods": len(period_ranges),
            "resolution_extents_utc": {
                f"{zone}:{resolution}": {
                    "first_period_start": min(
                        p["start"]
                        for p in period_ranges
                        if p["zone"] == zone and p["resolution"] == resolution
                    ),
                    "last_period_end": max(
                        p["end"]
                        for p in period_ranges
                        if p["zone"] == zone and p["resolution"] == resolution
                    ),
                }
                for zone, resolution in resolutions
            },
            "native_category_slots_by_resolution": {
                f"{zone}:{resolution}": n for (zone, resolution), n in resolutions.items()
            },
        },
    )


def check(rows, expected):
    actual_keys = Counter((r["zone"], r["start"]) for r in rows)
    actual = {(r["zone"], r["start"]): r["values"] for r in rows}
    common = actual.keys() & expected.keys()
    mismatch = 0
    maximum = 0
    null_mismatches = 0
    for key in common:
        left, right = actual[key], expected[key]
        if left.keys() != right.keys():
            mismatch += 1
        for name in left.keys() & right.keys():
            a, b = left[name], right[name]
            if (a is None) != (b is None):
                null_mismatches += 1
            elif a is not None:
                maximum = max(maximum, abs(a - b))
    return {
        "expected_native_or_quarter_intervals": len(expected),
        "source_only_intervals": len(expected.keys() - actual.keys()),
        "client_only_intervals": len(actual.keys() - expected.keys()),
        "duplicate_client_intervals": sum(n - 1 for n in actual_keys.values()),
        "category_column_mismatches": mismatch,
        "null_mismatches": null_mismatches,
        "max_abs_price_difference": maximum,
        "first_source_only_intervals": sorted(expected.keys() - actual.keys())[:8],
    }


def main():
    payload = {
        "method": "independent ElementTree A01/A03 native and quarter-hour coverage; untimed",
        "sets": {},
    }
    for name in REQUIRED:
        if not name.startswith("entsoe-imb-"):
            continue
        frozen = FrozenSet(DEFAULT_FIXTURES / name)
        native, quarters, evidence = publication(frozen)
        evidence["native_intervals"] = len(native)
        evidence["quarter_intervals"] = len(quarters)
        evidence["clients"] = {}
        for mode in ("nb-records", "nb-frame", "entsoe-py"):
            path = ROOT / ".jpcp/perf/runs" / name / f"{mode}.json"
            result = json.loads(path.read_text())
            if result["status"] == "PASS":
                expected = native if mode == "entsoe-py" else quarters
                evidence["clients"][mode] = check(result["rows"], expected)
            else:
                evidence["clients"][mode] = {"status": result["status"]}
        payload["sets"][name] = evidence
    Path("benchmarks/results/compare-source.json").write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
