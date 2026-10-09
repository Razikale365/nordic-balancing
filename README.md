# nordic-balancing

Python client for Nordic electricity **balancing** and **imbalance** data, normalised to
15-minute UTC intervals.

> **Status: pre-alpha.** Nothing is published yet; the API will change.

## Why

Nordic balancing data is spread across several transmission system operators and eSett,
each with its own API, authentication, time zone handling and resolution history. The
Nordic markets moved to 15-minute resolution in stages (imbalance settlement in 2023,
mFRR energy activation and intraday in 2025, day-ahead in October 2025), so the same
series can change resolution mid-history.

`nordic-balancing` aims to give one typed interface over these sources, with every series
in UTC on 15-minute intervals and a dated record of resolution and price-formula changes.

## Usage

```python
from datetime import UTC, datetime, timedelta

from nordic_balancing import BiddingZone, EnergiDataServiceClient

start = datetime(2026, 1, 15, tzinfo=UTC)
with EnergiDataServiceClient() as eds:
    prices = eds.imbalance_prices(start, start + timedelta(hours=1), [BiddingZone.DK1])

for p in prices:
    print(p.start, p.zone, p.imbalance_price_eur, p.dominating_direction)
```

Every query takes timezone-aware datetimes and returns intervals starting in
`[start, end)`, keyed by their UTC start. A price of `None` means the source has
not published it yet.

## Planned sources

| Source | Data | Access |
|---|---|---|
| Energinet — Energi Data Service | DK1/DK2 imbalance prices and components (**available**); mFRR/aFRR datasets | free, no key |
| Fingrid | FI imbalance and mFRR prices and bids | free API key |
| Svenska kraftnät | SE1–SE4 mFRR capacity and activation | free (CC-BY-4.0) |
| eSett | Nordic imbalance settlement | free |
| ENTSO-E Transparency Platform | balancing and imbalance series | free token |

Out of scope: generic day-ahead price clients and mFRR bid submission — other projects
already cover those.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m live      # calls the real APIs; not run by default
```

## Licence

MIT — see [LICENSE](LICENSE). Data retrieved through this library remains subject to each
source's own terms of use.
