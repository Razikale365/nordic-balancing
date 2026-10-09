"""Reconcile imbalance prices across providers on days around market changes.

Runs ``reconcile_imbalance_prices`` on Energi Data Service vs eSett (DK1, DK2)
and Fingrid vs eSett (FI) for six UTC days straddling the dated changes in
``nordic_balancing.CHANGES``:

  - 2025-03-10: Fingrid still publishes hourly imbalance prices;
  - 2025-03-19: first day of genuinely quarter-hourly imbalance prices;
  - 2025-06-02: Energinet's imbalance tolerance band takes effect;
  - 2025-10-02: a plain recent day;
  - 2026-03-04: first day of the satisfied-demand direction rule in Denmark;
  - 2026-09-15: a plain recent day.

EDS data starts 2025-03-04 and is skipped before that. Fingrid needs
``FINGRID_API_KEY`` in the environment; without it the FI pair is skipped.

Run: uv run python examples/reconciliation/reconcile_dk_fi.py
Writes results.json next to this file.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nordic_balancing import (
    BiddingZone,
    Divergence,
    EnergiDataServiceClient,
    ESettClient,
    FingridClient,
    ImbalancePrice,
    ReconciliationReport,
    reconcile_imbalance_prices,
)

DAYS = [
    datetime(2025, 3, 10, tzinfo=UTC),
    datetime(2025, 3, 19, tzinfo=UTC),
    datetime(2025, 6, 2, tzinfo=UTC),
    datetime(2025, 10, 2, tzinfo=UTC),
    datetime(2026, 3, 4, tzinfo=UTC),
    datetime(2026, 9, 15, tzinfo=UTC),
]
EDS_START = datetime(2025, 3, 4, tzinfo=UTC)
DK = (BiddingZone.DK1, BiddingZone.DK2)
RESULTS = Path(__file__).parent / "results.json"


def divergence_json(divergence: Divergence) -> dict[str, object]:
    """A JSON-friendly view of one divergence, keeping both records' values."""

    def fields(price: ImbalancePrice | None) -> dict[str, object]:
        if price is None:
            return {"price_eur": None, "resolution_min": None}
        return {
            "price_eur": price.imbalance_price_eur,
            "resolution_min": int(price.resolution.total_seconds() // 60),
        }

    return {
        "zone": divergence.zone.value,
        "start": divergence.start.isoformat(),
        "kind": divergence.kind.value,
        "detail": divergence.detail,
        "primary": fields(divergence.primary),
        "reference": fields(divergence.reference),
    }


def report_json(report: ReconciliationReport) -> dict[str, object]:
    return {
        "compared": report.compared,
        "identical": report.identical,
        "counts": {kind.value: count for kind, count in report.counts().items()},
        "divergences": [divergence_json(d) for d in report.divergences],
    }


def print_report(day: datetime, pair: str, report: ReconciliationReport) -> None:
    counts = " ".join(f"{kind.name}={count}" for kind, count in sorted(report.counts().items()))
    print(
        f"{day.date()} {pair}: compared={report.compared} "
        f"identical={report.identical} {counts or 'no divergences'}"
    )


def main() -> None:
    fingrid_key = os.environ.get("FINGRID_API_KEY")
    if not fingrid_key:
        print("FINGRID_API_KEY not set; skipping the fingrid vs esett pair")
    results: dict[str, dict[str, object]] = {}
    with ESettClient() as esett, EnergiDataServiceClient() as eds:
        fingrid = FingridClient() if fingrid_key else None
        try:
            for day in DAYS:
                end = day + timedelta(days=1)
                esett_prices = esett.imbalance_prices(day, end, (*DK, BiddingZone.FI))
                pairs: dict[str, ReconciliationReport] = {}
                if day >= EDS_START:
                    eds_prices = eds.imbalance_prices(day, end, DK)
                    pairs["energidataservice vs esett"] = reconcile_imbalance_prices(
                        eds_prices, esett_prices
                    )
                if fingrid is not None:
                    fingrid_prices = fingrid.imbalance_prices(day, end)
                    pairs["fingrid vs esett"] = reconcile_imbalance_prices(
                        fingrid_prices, esett_prices
                    )
                results[day.date().isoformat()] = {
                    pair: report_json(report) for pair, report in pairs.items()
                }
                for pair, report in pairs.items():
                    print_report(day, pair, report)
        finally:
            if fingrid is not None:
                fingrid.close()
    RESULTS.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"-> {RESULTS.name}")


if __name__ == "__main__":
    main()
