"""Replay a frozen imbalance-price dataset through ArcticGrid's battery model, offline.

Step 2 of the reference integration (see ``benchmarks/arcticgrid_reference/README.md``). It imports
only ArcticGrid and the standard library: no nordic-balancing, no network. It answers a data
question, not a financial one: does a downstream consumer stay correct when market inputs
contain unpublished intervals, DST days and resolution changes, and what does it cost to get
"unpublished" wrong?

The strategy is a deliberately naive threshold rule (charge at or below the dataset's 25th
percentile price, discharge at or above the 75th, hold otherwise). It is a probe for input
handling, not a trading strategy, and its EUR figures are not a revenue estimate: imbalance
prices are not a market a battery can simply trade into, and there are no fees, bids,
activation rules or forecast errors here.

Run (ArcticGrid is pinned by commit; nothing else is installed):
    uv run --no-project \
        --with "arcticgrid @ git+https://github.com/Razikale365/ArcticGrid@f7d4e64" \
        python benchmarks/arcticgrid_reference/replay_battery.py <dataset.json>
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from arcticgrid.domain.assets import Asset, AssetKind
from arcticgrid.domain.battery import Battery
from arcticgrid.domain.units import Energy, Power, StateOfCharge


def _battery() -> Battery:
    return Battery(
        asset=Asset(asset_id="ref-bess-1", site_id="ref-site", kind=AssetKind.BATTERY),
        capacity=Energy(kilowatt_hours=2000.0),
        max_charge=Power(kilowatts=1000.0),
        max_discharge=Power(kilowatts=1000.0),
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        min_soc=StateOfCharge(0.1),
        max_soc=StateOfCharge(0.9),
        soc=StateOfCharge(0.5),
    )


def _percentile(sorted_values: list[float], q: float) -> float:
    index = round(q * (len(sorted_values) - 1))
    return sorted_values[index]


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run(points: list[dict[str, Any]], *, null_as_zero: bool) -> dict[str, Any]:
    """Simulate the rule. ``null_as_zero`` models a consumer that mistakes unpublished for 0."""
    prices = sorted(p["price_eur_mwh"] for p in points if p["price_eur_mwh"] is not None)
    low, high = _percentile(prices, 0.25), _percentile(prices, 0.75)
    battery = _battery()
    cash = Decimal("0")
    actions: Counter[str] = Counter()
    trace = hashlib.sha256()
    previous_end: datetime | None = None
    gaps = overlaps = 0
    for point in points:
        start, end = _parse(point["start"]), _parse(point["end"])
        if previous_end is not None:
            gaps += start > previous_end
            overlaps += start < previous_end
        previous_end = end
        duration: timedelta = end - start
        price = point["price_eur_mwh"]
        if price is None:
            if not null_as_zero:
                actions["hold_unpublished"] += 1
                trace.update(f"{point['start']}|hold|{battery.soc.fraction:.9f}".encode())
                continue
            price = 0.0
        if price <= low:
            power = -battery.max_charge_power(duration).kilowatts
            action = "charge"
        elif price >= high:
            power = battery.max_discharge_power(duration).kilowatts
            action = "discharge"
        else:
            power, action = 0.0, "idle"
        battery = battery.apply_power(Power(kilowatts=power), duration)
        energy_mwh = Decimal(str(power)) * Decimal(duration.total_seconds()) / Decimal(3_600_000)
        cash += energy_mwh * Decimal(str(price))  # +discharge earns, -charge pays
        actions[action] += 1
        trace.update(f"{point['start']}|{action}|{battery.soc.fraction:.9f}".encode())
    return {
        "thresholds_eur_mwh": [low, high],
        "intervals": len(points),
        "actions": dict(sorted(actions.items())),
        "gaps": gaps,
        "overlaps": overlaps,
        "final_soc": round(battery.soc.fraction, 9),
        "cash_eur": str(cash.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)),
        "trace_sha256": trace.hexdigest(),
    }


def main() -> None:
    document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    points = document["points"]
    canonical = json.dumps(points, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != document["points_sha256"]:
        raise SystemExit("dataset hash mismatch: refusing to run on a modified dataset")
    by_day: Counter[str] = Counter(p["start"][:10] for p in points)
    resolutions = Counter(p["resolution_s"] for p in points)
    summary = {
        "dataset": document["dataset"],
        "library": document["library"],
        "points_sha256": document["points_sha256"],
        "resolutions_s": dict(sorted(resolutions.items())),
        "unpublished": sum(1 for p in points if p["price_eur_mwh"] is None),
        "utc_days_not_96_quarters": {d: n for d, n in sorted(by_day.items()) if n != 96},
        "correct_consumer": run(points, null_as_zero=False),
        "null_as_zero_consumer": run(points, null_as_zero=True),
    }
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
