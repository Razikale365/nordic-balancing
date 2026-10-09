# Release evidence

Compiled 2026-10-09, before the first PyPI release. Every claim cites a file,
a test, a command or a URL.

## Status

- Version `0.1.0a2` (`pyproject.toml`), a pre-release; **pre-alpha** — the API will change
  (classifier `Development Status :: 2 - Pre-Alpha`). Published on PyPI as pre-releases.
- Python `>=3.12` (`requires-python`); classifiers list 3.12 and 3.13.
- Licence: MIT (`LICENSE`). Retrieved data remains under each source's own terms.
- Runtime dependency: `httpx>=0.27,<1` only.

## Provider matrix

| Source | Data | Access | Verification | History start |
|---|---|---|---|---|
| Energi Data Service (Energinet) | DK1/DK2 imbalance prices and components | free, no key | live-verified: `test_live_one_hour_of_dk_imbalance_prices` | 2025-03-04 (`ImbalancePrice` dataset) |
| Fingrid | FI imbalance prices, mFRR up/down components, dominating direction (datasets 319, 244, 106, 369); generic `series(dataset_id)` | free key (`FINGRID_API_KEY`) | live-verified: `test_live_one_hour_of_fi_imbalance_prices`, `test_live_hourly_imbalance_prices_expand_to_quarters`, `test_live_overlap_hour_uses_the_hourly_value_settled_by_esett` | hourly publication until 2025-03-18; 15-minute records from 2025-03-14T23:15Z |
| Svenska kraftnät | SE1–SE4 mFRR/aFRR capacity market prices and volumes | free (CC-BY-4.0), no key | live-verified: `test_live_mfrr_one_hour_all_zones`, `test_live_afrr_one_hour` | hourly records; earliest hour UNVERIFIED |
| eSett | Imbalance prices, all 12 zones | free, no key | live-verified: `test_live_one_hour_of_se3_and_fi`, `test_live_hourly_to_quarter_hour_transition` | 2021-10-31T23:00Z |
| ENTSO-E Transparency Platform | Imbalance prices (document type A85), all 12 zones | free token (`ENTSOE_API_KEY`) | **schema-checked, not live** — `test_live_one_hour_of_se3_and_fi`, `test_live_one_day_of_dk1` skip without a token | UNVERIFIED |

## Observed API behaviour

Observed 2026-10-09 unless noted; file references are under
`src/nordic_balancing/sources/`.

### Energi Data Service

- `start`/`end` are read as Danish local time unless `timezone=UTC` is sent —
  a naive query silently shifts by an hour or two. The client always sends UTC
  (`energidataservice.py` module docstring).
- Query times are minute-precision; the client rounds outward and re-filters.
- Strict per-dataset rate limits: HTTP 429 with `Retry-After`; the shared
  retrier waits, then raises `RateLimitError` (`_http.py`).
- Offset/limit paging at 10 000 rows with a reported `total` that must match.
- vs eSett, DK 2025-03-10: 62/192 dominating-direction disagreements with
  identical prices; the cause is a hypothesis
  (`examples/reconciliation/README.md`).

### eSett

- `timestampUTC` must be UTC; records before 2023-05-21T22:00Z are hourly and
  repeated over four quarters (`esett.py`).
- Single-price model: `imblSalesPrice == imblPurchasePrice` is enforced.
- Requests are chunked at 31 days to bound all-zone responses.
- Genuine quarter-hour imbalance prices start 2025-03-18T23:00Z — not at the
  mFRR EAM go-live on 2025-03-04 (`examples/mfrr_transition/README.md`).
- Trailing unpublished intervals arrive as records with a null price, not as
  missing rows (`examples/entsoe_py_comparison/REPORT.md`, section 4).

### Fingrid

- API key sent as `x-api-key`; requests (including retries) are paced by
  `min_interval`, default 2 s (`fingrid.py`).
- Page 1 carries `pagination.total`/`lastPage`; the row count must match.
- Hourly publication ran until 2025-03-18 and overlapped the new 15-minute
  records from 2025-03-14T23:15Z. On 2025-03-16T09:00Z the hourly record said
  104, the quarters 107, and eSett settled 104 — so the client uses the hourly
  value before 2025-03-18T23:00Z and keeps the loser under
  `raw["<dataset>_superseded"]` (`fingrid.py`,
  `test_live_overlap_hour_uses_the_hourly_value_settled_by_esett`).

### Svenska kraftnät

- CKAN datastore API; the datastore resource id is discovered via
  `package_show` and cached (`svk.py`).
- One hourly record per zone and direction (up/down), repeated over quarters;
  `price_unit EUR-MW` and `volume_unit MW` are enforced.
- Queries filter on explicit hour lists, chunked at 168 h.

### ENTSO-E Transparency Platform

- Not live-verified (no token yet). The parser follows the official IEC 62325-451-6
  balancing document schema: element names, optional fields and code lists were
  checked against ENTSO-E's `CIM_xsd_package_v2026` (balancing v3.0 to v4.5). The
  test fixtures in `tests/fixtures/entsoe/` validate against those XSDs (balancing
  v3.0 and v4.5, acknowledgement v7.0).
- Schema detail that matters: the real element names are `imbalance_Price.amount`,
  `imbalance_Price.category` and `flowDirection.direction`. entsoe-py's parser
  lowercases tag names, so lowercase names copied from it match no real document.
  An earlier version of this client had that bug.
- One A85 request per zone per chunk of at most 7 days. Prices must be in EUR per MWH.
  `curveType` A01 and A03 are supported (A03 repeats the previous value for omitted
  positions), and A02, A04 and A05 raise. `PT60M` points are repeated over quarters.
- A Point without an amount gives `imbalance_price_eur=None`. `Financial_Price`
  components, categories, `flowDirection.direction` and the namespace are kept in
  `raw` but not interpreted. Equal dual-category prices collapse to one value; different
  ones raise. Acknowledgement documents, ZIP and plain XML bodies are handled, with a
  64 MiB cap.
- The two live tests skip without `ENTSOE_API_KEY`.

## Data contracts

See [README.md — Data contracts](../README.md#data-contracts) (C1–C8).
Not duplicated here.

## Dated market changes

`src/nordic_balancing/changes.py`, `CHANGES`, queried via `changes_between`:

1. 2023-05-21T22:00Z, all zones — imbalance settlement period changes
   60 → 15 minutes (observed in eSett).
2. 2025-03-14T23:15Z, FI — Fingrid switched to 15-minute publication for
   datasets 319, 244, 106, 369; hourly records overlapped until
   2025-03-18T23:00Z, sometimes with different values.
3. 2025-03-18T23:00Z, all zones — genuine 15-minute imbalance prices replace
   the hourly price repeated per quarter (observed in eSett).
4. 2025-03-18T23:00Z, DK — new Danish imbalance price design; before this the
   imbalance price equalled the Pricing Module imbalance price.
5. 2026-03-03T23:00Z, DK — dominating direction determined by satisfied
   demand; before this by balancing demand.

## Known limitations

- `resolution` records publication cadence, not information content: quarters
  before 2025-03-18T23:00Z are labelled 15 min although the four quarters of an
  hour carried a single hourly value (`examples/mfrr_transition/README.md`;
  `examples/entsoe_py_comparison/REPORT.md`, "Gaps found").
- `None` conflates "not yet published" with "not provided by this source"
  (`models.py` docstrings; same REPORT section).
- `EntsoeClient` is schema-checked but not live-verified. Its strict parser raises
  `SourceError` on dual-price TimeSeries and on curve types A02/A04/A05, which
  entsoe-py may tolerate.
- A possible FI imbalance-price formula change around 2026-06-01 is UNVERIFIED
  (no primary source found): it has no entry in `CHANGES` and no test coverage.
- No async client; all clients are synchronous over `httpx.Client`.
- Python 3.12+ only.
- eSett history starts 2021-10-31T23:00Z; nothing earlier exists in this API.
- Reconciliation classifies differences only; it never merges series or picks
  a source (`reconcile_imbalance_prices`, README).
- `raw` is a read-only mapping over a deep copy private to each record. Only the
  top level rejects writes; nested containers are mutable but unshared, so treat
  them as read-only.
- Fingrid `series()` has a verified hourly policy only for datasets 319, 244, 106
  and 369 (per-interval prices/direction). Hourly records from any other dataset
  raise `SourceError` rather than being repeated, since they may be additive totals.
- eSett: a price published in only one of the sales/purchase columns is taken as
  the single price (never observed in ~107k records sampled 2021-11 to 2026-10).
- Energi Data Service: `DominatingDirection` must be exactly -1, 0 or 1 (observed
  values in ~16.8k records: only those and null).

## Test coverage

- Offline suite (default; `addopts` deselects `live`): **370 tests**, all
  passing on 2026-10-09.
- Live suite (`uv run pytest -m live`): **10 tests**. Verified 2026-10-09:
  8 passed (EDS 1, eSett 2, Fingrid 3, SvK 2); the 2 ENTSO-E tests skipped —
  no `ENTSOE_API_KEY`. The Fingrid tests skip without `FINGRID_API_KEY`.

| File | Offline | Live |
|---|---:|---:|
| `tests/test_changes.py` | 7 | — |
| `tests/test_energidataservice.py` | 44 | 1 |
| `tests/test_entsoe.py` | 77 | 2 |
| `tests/test_esett.py` | 59 | 2 |
| `tests/test_fingrid.py` | 90 | 3 |
| `tests/test_models.py` | 5 | — |
| `tests/test_package.py` | 1 | — |
| `tests/test_reconcile.py` | 27 | — |
| `tests/test_svk.py` | 60 | 2 |

`uv run pytest --co -q | tail -1`:

```
370/380 tests collected (10 deselected)
```

Run offline: `uv run pytest -q`. Run live: `uv run pytest -m live` — keys go in
the environment or a repo `.env` (see `.env.example`; `tests/conftest.py`
loads it). No coverage tool is configured.

## Reproducible evidence

| Script | Command | Shows |
|---|---|---|
| `examples/reconciliation/reconcile_dk_fi.py` | `uv run python examples/reconciliation/reconcile_dk_fi.py` | EDS and Fingrid vs eSett over six sampled days; writes `results.json`. FI pair needs `FINGRID_API_KEY` |
| `examples/mfrr_transition/analysis.py` | `uv run --with matplotlib python examples/mfrr_transition/analysis.py` | When quarter-hourly prices became real (2025-03-18T23:00Z); caches eSett data in `cache.json` |
| `examples/entsoe_py_comparison/nordic_quirks.py` | `uv run python examples/entsoe_py_comparison/nordic_quirks.py` | Live eSett: the 2023-05 resolution switch, repeated hourly prices, unpublished intervals |
| `examples/entsoe_py_comparison/repro_issue_529.py` | `uv run --with entsoe-py==0.8.1 python examples/entsoe_py_comparison/repro_issue_529.py` | Offline repro of entsoe-py bug #529 (35,519 vs 35,520 rows); upstream fix PR #539 open |
| `examples/entsoe_py_comparison/live_side_by_side.py` | `uv run --with entsoe-py==0.8.1 python examples/entsoe_py_comparison/live_side_by_side.py` | Three-way diff nordic-balancing vs entsoe-py vs eSett; exits early without `ENTSOE_API_KEY` |

Each script's documented output is in the README/REPORT next to it.
