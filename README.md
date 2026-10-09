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

from nordic_balancing import BiddingZone, ESettClient, EnergiDataServiceClient

start = datetime(2026, 1, 15, tzinfo=UTC)
with EnergiDataServiceClient() as eds:
    prices = eds.imbalance_prices(start, start + timedelta(hours=1), [BiddingZone.DK1])

with ESettClient() as esett:
    nordic_prices = esett.imbalance_prices(start, start + timedelta(hours=1))  # all 12 zones

for p in prices:
    print(p.start, p.zone, p.imbalance_price_eur, p.dominating_direction)
```

Every query takes timezone-aware datetimes and returns intervals starting in
`[start, end)`, keyed by their UTC start. A price of `None` means the source has
not published it yet or does not provide it. `resolution` retains the source's
publication interval; historical hourly values are expanded into four quarters.
`raw` is a read-only mapping over a deep copy of the source record, private to
that record; only its top level rejects writes, so treat nested values as read-only too.

Set `FINGRID_API_KEY` to a free key from https://developer-data.fingrid.fi.
Use `with FingridClient() as fingrid:` after importing it from `nordic_balancing`.
Call `fingrid.imbalance_prices(start, end)` for FI prices, or `series(dataset_id, start, end)`.
`series()` expands hourly records only for datasets 319, 244, 106 and 369; any other
dataset must be 15-minute, and an hourly record raises `SourceError`.

Svenska kraftnät needs no key.
Call `SvKClient().capacity_market(ReserveProduct.MFRR, start, end)` for SE1–SE4 capacity prices and volumes.

## Data contracts

Every client enforces the same contracts; a violation raises `SourceError`.

- **C1 Window.** Queries take aware datetimes and return exactly the intervals
  whose start is in `[start, end)`, at any sub-second precision of `start`/`end`.
- **C2 Pagination.** Rows fetched must equal the total the source reported; a
  missing or invalid total, a short or empty page before the total is reached,
  or a mismatch is an error.
- **C3 Duplicates.** Two source records for the same series key (zone and
  interval start, plus product and direction for SvK) are an error, even when
  the values are equal. ENTSO-E is the exception: an interval published under
  several price categories with one equal price collapses to that price, with
  every category kept in `raw`; unequal prices, or equal prices at different
  resolutions, raise.
- **C4 Missing vs unpublished.** An interval the source did not return is absent
  from the result; a record returned with a null value has that field set to
  `None`. Clients never fill gaps.
- **C5 Numbers.** Values must be JSON numbers (`bool` rejected), finite and
  convertible to `float`.
- **C6 Outside window.** Records outside the requested query window are an error.
- **C7 Resolution.** `resolution` is the source's publication interval; hourly
  records are repeated across four quarters.
- **C8 Transport.** Network and protocol failures surface as `SourceError`, never as
  raw `httpx` exceptions. HTTP 429/503 are retried, then raise `RateLimitError`, which
  is raised at once when `Retry-After` exceeds 300 s. HTTP 502/504, timeouts and
  dropped connections are retried within the same `max_retries` budget, then raise
  `SourceError`.

`reconcile_imbalance_prices` reports differences between two sources; it never merges
series or picks a source. Zones present in only one input are listed in
`primary_only_zones` / `reference_only_zones` rather than dropped silently.

See [docs/RELEASE_EVIDENCE.md](docs/RELEASE_EVIDENCE.md) for provider verification
status, observed API behaviour and known limitations.

## Planned sources

| Source | Data | Access |
|---|---|---|
| Energinet — Energi Data Service | DK1/DK2 imbalance prices and components (**available**); mFRR/aFRR datasets | free, no key |
| Fingrid | FI imbalance prices, mFRR price components and 15-minute series (**available**) | free API key |
| Svenska kraftnät | SE1–SE4 mFRR and aFRR capacity market prices and volumes (hourly, expanded to 15 minutes) (**available**) | free (CC-BY-4.0) |
| eSett | Imbalance prices, all 12 Nordic zones, from 2021-11-01 (**available**) | free, no key |
| ENTSO-E Transparency Platform | Imbalance prices (A85), all 12 Nordic zones (**available, not yet live-verified**) | free token (`ENTSOE_API_KEY`) |

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

This is an independent project. It is not affiliated with, endorsed by or supported by
Energinet, Fingrid, Svenska kraftnät, eSett, ENTSO-E or any other data provider.
