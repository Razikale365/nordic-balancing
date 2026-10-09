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

## Planned sources

| Source | Data | Access |
|---|---|---|
| Energinet — Energi Data Service | DK1/DK2 imbalance prices, mFRR/aFRR | free, no key |
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
```

## Licence

MIT — see [LICENSE](LICENSE). Data retrieved through this library remains subject to each
source's own terms of use.
