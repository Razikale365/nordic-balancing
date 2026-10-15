# Release evidence

Originally compiled 2026-10-09, before the first PyPI release; updated 2026-10-12
for the 0.1.0 ENTSO-E live verification and again after it for the committed
ENTSO-E validation script; updated 2026-10-15 for 0.2.0. Each live result carries the
date it was observed. Every claim cites a file, a test, a command or a URL.

## Status

- Version `0.2.0` (`pyproject.toml`); **beta** (classifier `Development Status :: 4 -
  Beta`). The public surface is defined and tested (`docs/API_CONTRACT.md`,
  `tests/test_public_api.py`); before 1.0 it can still change between minor versions,
  and any such change is listed in the CHANGELOG. 0.1.0 was the first non-pre-release;
  0.1.0a1–a3 were PyPI pre-releases.
- Python `>=3.12` (`requires-python`); classifiers list 3.12, 3.13 and 3.14, and CI
  runs all three plus a job at the lowest declared dependency versions.
- Scheduled live runs: `.github/workflows/live.yml` runs `pytest -m live` daily at
  09:17 UTC with repository secrets. The first scheduled run, 2026-10-15
  ([38749451136](https://github.com/Razikale365/nordic-balancing/actions/runs/38749451136)),
  passed 17 of 17 with none skipped.
- Licence: MIT (`LICENSE`). Retrieved data remains under each source's own terms.
- Runtime dependency: `httpx>=0.27,<1` only. Optional extra `pandas` (`pandas>=2.1`), used
  only by `to_frame`; CI runs the suite at the lowest allowed version (pandas 2.1, the
  `lowest-direct` job in `.github/workflows/ci.yml`) and at the locked version (pandas
  3.0.6, `uv.lock`).

## Provider matrix

| Source | Data | Access | Verification | History start |
|---|---|---|---|---|
| Energi Data Service (Energinet) | DK1/DK2 imbalance prices and components | free, no key | live-verified: `test_live_one_hour_of_dk_imbalance_prices` | 2025-03-04 (`ImbalancePrice` dataset) |
| Fingrid | FI imbalance prices, mFRR up/down components, dominating direction (datasets 319, 244, 106, 369); generic `series(dataset_id)` | free key (`FINGRID_API_KEY`) | live-verified: `test_live_one_hour_of_fi_imbalance_prices`, `test_live_hourly_imbalance_prices_expand_to_quarters`, `test_live_overlap_hour_uses_the_hourly_value_settled_by_esett` | hourly publication until 2025-03-18; 15-minute records from 2025-03-14T23:15Z |
| Svenska kraftnät | SE1–SE4 mFRR/aFRR capacity market prices and volumes | free (CC-BY-4.0), no key | live-verified: `test_live_mfrr_one_hour_all_zones`, `test_live_afrr_one_hour` | hourly records; earliest hour UNVERIFIED |
| eSett | Imbalance prices, all 12 zones | free, no key | live-verified: `test_live_one_hour_of_se3_and_fi`, `test_live_hourly_to_quarter_hour_transition` | 2021-10-31T23:00Z |
| ENTSO-E Transparency Platform | Imbalance prices (document type A85), all 12 zones | free token (`ENTSOE_API_KEY`) | live-verified (2026-10-12): `test_live_one_hour_of_se3_and_fi`, `test_live_one_day_of_dk1`, `test_live_matches_esett_prices_but_not_its_resolution_label` | 2021-10-31T23:00Z (FI 2021-10-31T22:00Z) |

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

- Live-verified 2026-10-12 against eSett on 60 zone-windows (5 UTC windows × 12
  zones, 72 zone-days, 6,912 intervals): 2022-01-15, 2023-05-21T12:00Z+24h (the
  60 → 15 min settlement switch), 2025-03-18T12:00Z+24h, 2025-10-25T22:00Z+24h (DST
  end) and 2026-10-10..11. Every interval was present in both sources and no price was
  `None`. Prices matched exactly in 10 zones; DK1/DK2 differed by 0.01 EUR in 16
  intervals, at most 5 per zone and window (rounding, the same size as the EDS vs eSett
  differences in `examples/reconciliation/README.md`). Rerun with
  `examples/entsoe_validation/validate.py samples --live`: same result
  (`results_samples.json`). SE3 and FI on
  2026-01-15 also match entsoe-py 0.8.1 exactly
  (`examples/entsoe_py_comparison/live_side_by_side.py`).
- Observed documents: every A85 response was a ZIP (`application/zip`) with one
  schema-4.4 document, `area_Domain.mRID` equal to the requested EIC, `curveType` A03,
  two TimeSeries per Period (categories A04 and A05) with equal prices, and no
  `flowDirection.direction`. No point fell outside the requested window. Windows before
  the history start return no records.
- `resolution` labels differ from eSett although the prices agree. ENTSO-E publishes
  `PT15M` from 2023-05-21T00:00Z in FI, from 2025-03-03T00:00Z in DK1/DK2 and from
  2025-03-18T00:00Z in NO1–NO5 and SE1–SE4; eSett from 2023-05-21T22:00Z everywhere.
  Neither date marks genuine quarter-hour prices (2025-03-18T23:00Z). Between those
  dates `reconcile_imbalance_prices` reports RESOLUTION divergences for ENTSO-E vs eSett
  (`test_live_matches_esett_prices_but_not_its_resolution_label` pins the SE3 boundary).
- History start: FI's first interval is 2021-10-31T22:00Z (local midnight, UTC+2),
  every other zone's 2021-10-31T23:00Z (checked live 2026-10-12 for FI and SE3 over
  2021-10-31T20:00Z–2021-11-01T02:00Z: 16 and 12 intervals).
- Observed request latency did not depend on the window length: 2–42 s for one
  hour, 4–21 s for 31 to 367 days (one zone, observed 2026-10-12; a 367-day window was
  accepted). The client therefore asks for up to 366 days per request and runs up to
  `max_workers` (default 12) requests in parallel. 12 zones × 2025 took 90 s, against
  358 s one request at a time; with the earlier 7-day chunks it would have been 624
  requests.
- Full year 2025 vs eSett (420,480 intervals, each in
  exactly one category; `examples/entsoe_validation/validate.py year-2025 --live`,
  rerun 2026-10-12 with the same counts as the first, uncommitted probe; per-zone
  counts and every differing interval in `results_year-2025.json`):
  - 7,361 missing in ENTSO-E. DK1/DK2 lack 2025-01-05T23:00Z–2025-01-30T08:45Z and
    about 1,200 scattered quarters (most in April–May); NO1–NO5 and SE1–SE4 lack
    12–20 quarters each, mainly 2025-01-06T16:00–17:45Z and the hour from
    2025-01-22T23:00Z; FI lacks none. Nothing is missing in eSett, and no price is
    `None` on either side.
  - 411,972 exact; 1,057 differ by at most 0.01 EUR (rounding, all DK1/DK2).
  - 86 DK quarters (43 per zone, mostly 2025-04-01/02) read 0.0 where eSett has a
    price.
  - 4 differ by more than 0.01 otherwise: DK1 2025-06-29T17:45Z (2141.93 vs 59.70),
    FI 2025-04-29T02:30Z, NO3/NO4 2025-04-24T18:45Z. The 0.1.0 text listed only the
    3 outside DK.
  - The DK1 2025-06-29T17:45Z value is not a parsing error (checked live 2026-10-12).
    Energi Data Service also publishes `ImbalancePriceEUR` 2141.93, which equals that
    quarter's aFRR up price (EDS `aFRRVWAUpEUR`, eSett `upRegPriceFrrA`). eSett's
    imbalance price, 59.70, equals its mFRR up price and the spot price. The sources
    disagree on which price sets the imbalance price for that quarter; which one is
    authoritative is not established here.
  - entsoe-py 0.8.1 shows the same 36 missing quarters and 37 zero rows for DK1 on
    2025-04-02/03, so these are in the published documents. The client reports every
    case above as published (contract C4), and `reconcile_imbalance_prices` against
    eSett finds them.
- Across a chunk boundary (2026-10-15; `examples/entsoe_validation/validate.py
  multi-chunk --live`, `results_multi-chunk.json`): DK1 and SE3, 2024-06-01 to
  2025-07-01 (395 days), two requests per zone split at 2025-06-02T00:00Z. Of 75,840
  intervals, 71,989 exact, 189 rounding (DK1), 43 zero_price (DK1, the 2025-04-01/02
  quarters above), 1 material (DK1 2025-06-29T17:45Z, above) and 3,618 missing in
  ENTSO-E (DK1 3,570, mostly the January 2025 gap; SE3 48, whole hours between
  2024-09 and 2025-02). Nothing is missing in eSett. Around the boundary the result
  equals a single request per zone (768 intervals, identical).
- The parser follows the official IEC 62325-451-6
  balancing document schema: element names, optional fields and code lists were
  checked against ENTSO-E's `CIM_xsd_package_v2026` (balancing v3.0 to v4.5). The
  test fixtures in `tests/fixtures/entsoe/` validate against those XSDs (balancing
  v3.0 and v4.5, acknowledgement v7.0).
- Schema detail that matters: the real element names are `imbalance_Price.amount`,
  `imbalance_Price.category` and `flowDirection.direction`. entsoe-py's parser
  lowercases tag names, so lowercase names copied from it match no real document.
  An earlier version of this client had that bug.
- One A85 request per zone per chunk of at most 366 days, in parallel. Prices must be in EUR per MWH.
  `curveType` A01 and A03 are supported (A03 repeats the previous value for omitted
  positions), and A02, A04 and A05 raise. `PT60M` points are repeated over quarters.
- A Point without an amount gives `imbalance_price_eur=None`. `Financial_Price`
  components, categories, `flowDirection.direction` and the namespace are kept in
  `raw` but not interpreted. Equal dual-category prices collapse to one value; different
  ones raise. Acknowledgement documents, ZIP and plain XML bodies are handled, with a
  64 MiB cap.
- Checks added in the second audit, also taken from the XSDs:
  - The document-level area is `controlArea_Domain.mRID` in v3.0–v4.0 and
    `area_Domain.mRID` from v4.1. It is optional in all versions, and when present it
    must equal the requested EIC.
  - The price unit is `price_Measure_Unit.name` up to v4.4 and
    `price_Measurement_Unit.name` in v4.5. Both are checked.
  - `type` is mandatory and must be A85; a document without it raises.
  - A Period must be a whole number of resolution steps.
  - An interval published at two resolutions raises, even with equal prices.
- The four ENTSO-E live tests (three in `tests/test_entsoe.py`, one in
  `tests/test_live_recent.py`) skip without `ENTSOE_API_KEY`.

## Memory and timing

One year, all 12 zones, 420,480 records, measured offline with synthetic responses
(`examples/benchmarks/memory.py`; method, machine and full table in
`examples/benchmarks/README.md`; medians of three runs, 2026-10-15):

| Scenario | Wall s | Peak RSS MiB | Traced peak MiB | Retained MiB |
|---|---:|---:|---:|---:|
| eSett 2025 | 78.0 | 1,344 | 401 | 330 |
| eSett 2022 | 44.4 | 816 | 274 | 237 |
| ENTSO-E 2025 | 266.5 | 2,301 | 906 | 492 |
| ENTSO-E 2022 | 159.2 | 1,515 | 567 | 449 |

The benchmark flags a scenario when peak resident memory exceeds 1.5 GiB or 3 KiB per
record (`memory.py`). By that rule ENTSO-E 2025 crosses both (2,301 MiB, 5,739 B per
record), and eSett 2025 and ENTSO-E 2022 cross the per-record figure.

Decision (maintainer, 2026-10-14; unchanged by the 2026-10-15 rerun): keep the
synchronous, list-returning API for 0.2.0. The memory the library itself holds,
measured as the traced peak, is under both limits in every scenario (worst: ENTSO-E
2025, 906 MiB, about 2.3 KB per record). Most of the resident excess is the C-level
XML parser working on up to 12 parallel requests, which ends when the call returns.
A caller that needs less memory can fetch fewer zones or a shorter window per call,
or pass a lower `max_workers` to `EntsoeClient`. An option to drop `raw`, or a lower
default `max_workers`, will be considered if a user reports memory as a problem.
Wall times exclude network latency, which dominates live ENTSO-E calls.

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
- `None` has one meaning per source and field: a field a source does not provide
  is `None` on every record from that source; in a field it provides, `None` means
  the source returned no value. `docs/FIELD_MATRIX.md` lists which fields each
  source provides and how it shows an unpublished interval, and
  `tests/test_field_matrix.py` enforces it. The library cannot tell "not yet
  published" from "never published" where the source does not.
- `EntsoeClient`'s strict parser raises `SourceError` on differing dual-category prices
  and on curve types A02/A04/A05, which entsoe-py may tolerate. None of these occurred
  in the 60 live zone-windows checked.
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
- ENTSO-E checks that never triggered on live data (60 zone-windows and all 12 zones for
  2025, 2026-10-12) but stay strict, so they fail loudly if the API changes:
  - A point outside the requested window raises. entsoe-py truncates its A85 results
    to the window; no extra points were observed.
  - A TimeSeries without `curveType` raises, although the element is optional in the
    XSD; every observed TimeSeries had A03.
  - The response area must equal the bidding-zone EIC sent as `controlArea_Domain`;
    it did in every observed document.
- ENTSO-E: the token is a query parameter, as the API requires. If you enable INFO
  logging for the `httpx` logger, request URLs are logged, and with them the token.
- SvK: the client asks for on-the-hour starts and expands each record to four
  quarters. Every record observed so far is hourly (latest checked:
  2026-10-09T21:00Z), and the dataset has no duration column. If SvK moves to
  15-minute capacity records, the change would not be detected.

## Test coverage

- Offline suite (default; `addopts` deselects `live`): 660 tests, all passing on
  2026-10-15. `tests/test_properties.py` holds Hypothesis property tests: for every
  client, splitting the window gives the same records, and every start is on the
  15-minute grid in `[start, end)` with none missing; it also checks reconciliation
  symmetry. `tests/test_public_api.py` fails on any change to the public surface, and
  `tests/test_field_matrix.py` enforces `docs/FIELD_MATRIX.md`.
- Live suite (`uv run pytest -m live`): 17 tests. The Fingrid tests skip without
  `FINGRID_API_KEY`, the ENTSO-E tests without `ENTSOE_API_KEY`;
  `tests/test_live_recent.py` checks a recent window for each source daily.
- Test counts show what runs, not that it is correct. Count them with:

```
uv run pytest --co -q | tail -1
660/677 tests collected (17 deselected)
```

Run offline: `uv run pytest -q`. Run live: `uv run pytest -m live` — keys go in
the environment or a repo `.env` (see `.env.example`; `tests/conftest.py`
loads it). Branch coverage is measured on demand, not enforced:
`uv run --with pytest-cov pytest -q --cov=nordic_balancing --cov-branch`.

## Reproducible evidence

| Script | Command | Shows |
|---|---|---|
| `examples/reconciliation/reconcile_dk_fi.py` | `uv run python examples/reconciliation/reconcile_dk_fi.py` | EDS and Fingrid vs eSett over six sampled days; writes `results.json`. FI pair needs `FINGRID_API_KEY` |
| `examples/mfrr_transition/analysis.py` | `uv run --with matplotlib python examples/mfrr_transition/analysis.py` | When quarter-hourly prices became real (2025-03-18T23:00Z); caches eSett data in `cache.json` |
| `examples/entsoe_py_comparison/nordic_quirks.py` | `uv run python examples/entsoe_py_comparison/nordic_quirks.py` | Live eSett: the 2023-05 resolution switch, repeated hourly prices, unpublished intervals |
| `examples/entsoe_py_comparison/repro_issue_529.py` | `uv run --with entsoe-py==0.8.1 python examples/entsoe_py_comparison/repro_issue_529.py` | Offline repro of entsoe-py bug #529 (35,519 vs 35,520 rows); upstream fix PR #539 open |
| `examples/entsoe_validation/validate.py` | `uv run python examples/entsoe_validation/validate.py samples --live` (or `year-2025 --live`, `multi-chunk --live`) | ENTSO-E vs eSett (all 12 zones; DK1 and SE3 across a chunk boundary for `multi-chunk`), every interval in one category; writes `results_<mode>.json`. Needs `ENTSOE_API_KEY`; does nothing without `--live` |
| `examples/entsoe_py_comparison/live_side_by_side.py` | `uv run --with entsoe-py==0.8.1 python examples/entsoe_py_comparison/live_side_by_side.py` | Three-way diff nordic-balancing vs entsoe-py vs eSett (SE3, FI, 2026-01-15): 96/96 rows, 0 differences on 2026-10-12; exits early without `ENTSOE_API_KEY` |
| `examples/benchmarks/memory.py` | `uv run --with psutil python examples/benchmarks/memory.py` | Peak memory and wall time for one year, 12 zones, eSett and ENTSO-E; offline, synthetic responses, no keys |

Each script's documented output is in the README/REPORT next to it.
