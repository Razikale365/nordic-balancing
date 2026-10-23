# nordic-balancing

[![PyPI](https://img.shields.io/pypi/v/nordic-balancing)](https://pypi.org/project/nordic-balancing/)
[![CI](https://github.com/Razikale365/nordic-balancing/actions/workflows/ci.yml/badge.svg)](https://github.com/Razikale365/nordic-balancing/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/nordic-balancing)](https://pypi.org/project/nordic-balancing/)

Typed Python clients for Nordic electricity **balancing** data: imbalance prices and reserve
capacity from eSett, Energinet, Fingrid, Svenska kraftnät and ENTSO-E, all on 15-minute UTC
intervals, with every source quirk handled explicitly instead of silently.

## Why it exists: the data is not what it says

eSett has published Nordic imbalance prices every 15 minutes since May 2023. But until
**19 March 2025** the four quarters of every hour carried the same price: across 192,108
zone-hours before that date, not one had differing quarters. Since then they differ in most
hours; in DK, FI, SE3 and SE4 the average spread within an hour is over 100 EUR/MWh.

![Share of hours where all four quarter-hour imbalance prices match, December 2024 to May 2025](https://raw.githubusercontent.com/Razikale365/nordic-balancing/main/examples/mfrr_transition/summary.png)

A backtest of quarter-hour flexibility, or a model trained across that date, would treat
repeated hourly prices as real quarter-hour ones. The switch is also not the mFRR energy
market go-live on 4 March 2025 that most people would guess.
[Full analysis](https://github.com/Razikale365/nordic-balancing/blob/main/examples/mfrr_transition/README.md).

`nordic-balancing` records that kind of change as data. Every record keeps the resolution the
source published at, and `changes_between(start, end)` returns the dated market changes
(resolution switches, price-formula changes) inside any window.

## Install

```bash
pip install nordic-balancing            # httpx is the only dependency
pip install "nordic-balancing[pandas]"  # adds to_frame()
```

Python 3.11 or newer.

## Quick start

```python
from datetime import UTC, datetime, timedelta

from nordic_balancing import ESettClient, to_frame

start = datetime(2026, 1, 15, tzinfo=UTC)
with ESettClient() as esett:
    prices = esett.imbalance_prices(start, start + timedelta(days=1))  # all 12 zones

for p in prices[:4]:
    print(p.start, p.zone, p.imbalance_price_eur, p.dominating_direction)

frame = to_frame(prices).set_index(["start", "zone"])  # needs the pandas extra
```

## Which client to use

| You want | Use | Key |
|---|---|---|
| Imbalance prices for all 12 Nordic zones | `ESettClient().imbalance_prices(start, end)` | none |
| The same prices from ENTSO-E, e.g. to cross-check | `EntsoeClient().imbalance_prices(start, end)` | `ENTSOE_API_KEY` |
| DK1/DK2 imbalance prices with their components | `EnergiDataServiceClient().imbalance_prices(start, end, zones)` | none |
| FI imbalance prices and mFRR components, or any Fingrid dataset | `FingridClient().imbalance_prices(start, end)`, `.series(dataset_id, start, end)` | `FINGRID_API_KEY` |
| Reserve capacity (FCR, aFRR, mFRR) prices and volumes | `capacity_market(product, start, end)` on: `SvKClient` (SE mFRR, aFRR), `EnergiDataServiceClient` (DK; aFRR for all zones; FCR for SE), `FingridClient` (FI), `EntsoeClient` (NO; FI mFRR) | as above |
| Where two sources disagree | `reconcile_imbalance_prices(primary, reference)` | none |

Keys are free: ENTSO-E from the [Transparency Platform](https://transparency.entsoe.eu/),
Fingrid from [developer-data.fingrid.fi](https://developer-data.fingrid.fi). Coverage and
history start per source are in [docs/FIELD_MATRIX.md](https://github.com/Razikale365/nordic-balancing/blob/main/docs/FIELD_MATRIX.md) and
[docs/SOURCE_NOTES.md](https://github.com/Razikale365/nordic-balancing/blob/main/docs/SOURCE_NOTES.md).

## Data contracts

Every client enforces the same rules and raises `SourceError` when a source breaks them.
The full text is in [docs/API_CONTRACT.md](https://github.com/Razikale365/nordic-balancing/blob/main/docs/API_CONTRACT.md).

- **Exact windows.** Timezone-aware `[start, end)` in, exactly the intervals starting in it out.
- **No gap filling.** An interval the source did not return is absent; a value it has not
  published is `None`, never `0`.
- **Duplicates raise**, even with equal values, except for a few documented source patterns.
- **Pagination is checked** against the total the source reports.
- **Numbers are validated**: finite JSON numbers only.
- **Resolution is kept**: hourly values are expanded to four quarters and labelled hourly.
- **Transport errors are typed**: retries for 429/5xx/timeouts, then `RateLimitError` or
  `SourceError`, never a raw `httpx` exception.

`reconcile_imbalance_prices` reports differences between two sources by kind (rounding,
price, direction, resolution, missing, unpublished); it never merges series or picks a winner.

## What it has found in the real data

Each is reproducible with a script in [examples/](https://github.com/Razikale365/nordic-balancing/tree/main/examples).

- **ENTSO-E is missing 7,361 zone-quarters of 2025** that eSett has, and 86 DK quarters read
  `0.0` where eSett has a price ([entsoe_validation](https://github.com/Razikale365/nordic-balancing/blob/main/examples/entsoe_validation/README.md)).
- **eSett marks unsettled DK intervals with `0.0`** in every price column instead of null.
  This library returns `None` for them, as for any unpublished value.
- **Reading "unpublished" as zero is invisible downstream.** In a battery replay on
  [ArcticGrid](https://github.com/Razikale365/nordic-balancing/blob/main/benchmarks/arcticgrid_reference/README.md), one unpublished quarter read as
  0 EUR/MWh changed the dispatch but left the cash total identical.
- **Energi Data Service reads query times as Danish local time** unless told otherwise, so a
  naive query shifts by one or two hours.
- **Fingrid's hourly and 15-minute series overlapped for four days in March 2025** with
  different values; the client keeps the hourly value, which is the one eSett settled.
- **ENTSO-E labels SE and NO prices hourly until 2025-03-18** although eSett has published them
  per quarter since 2023; the prices agree, the labels do not.
- **A dropped row in entsoe-py** at its yearly query boundary
  ([issue #529](https://github.com/EnergieID/entsoe-py/issues/529)), reproduced offline
  ([entsoe_py_comparison](https://github.com/Razikale365/nordic-balancing/blob/main/examples/entsoe_py_comparison/REPORT.md)).

All observed source behaviour, with dates, is in [docs/SOURCE_NOTES.md](https://github.com/Razikale365/nordic-balancing/blob/main/docs/SOURCE_NOTES.md).

## Status

Release candidate for 1.0 (`1.0.0rc1`) on [PyPI](https://pypi.org/project/nordic-balancing/);
install it with `pip install --pre nordic-balancing`, since pip skips pre-releases by default. The
public API is pinned by a snapshot test against [docs/API_CONTRACT.md](https://github.com/Razikale365/nordic-balancing/blob/main/docs/API_CONTRACT.md), and
from 1.0 it follows Semantic Versioning. Every source is checked against its live API daily. Performance, including a comparison with entsoe-py and
esett-py on identical recorded responses, is in [benchmarks/](https://github.com/Razikale365/nordic-balancing/blob/main/benchmarks/README.md).

Not covered yet: activated balancing energy volumes. Out of scope: day-ahead price clients and
mFRR bid submission, which other projects already cover.

## Development

```bash
uv sync
uv run pytest              # offline suite
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -m live      # calls the real APIs; needs the keys above
```

See [CONTRIBUTING.md](https://github.com/Razikale365/nordic-balancing/blob/main/CONTRIBUTING.md) and [SECURITY.md](https://github.com/Razikale365/nordic-balancing/blob/main/SECURITY.md).

## Licence

The code is MIT-licensed, see [LICENSE](https://github.com/Razikale365/nordic-balancing/blob/main/LICENSE).

The data is not covered by that licence; it stays under each provider's own terms. Energinet,
Svenska kraftnät and Fingrid publish under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
so if you republish their data you must credit the provider.

This is an independent project, not affiliated with or endorsed by Energinet, Fingrid,
Svenska kraftnät, eSett, ENTSO-E or any other data provider.
