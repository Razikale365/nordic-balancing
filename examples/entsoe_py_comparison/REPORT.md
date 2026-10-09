# nordic-balancing vs entsoe-py: evidence comparison

Date: 2026-10-09. Compared: `nordic-balancing` 0.1.0.dev0 (this repo) vs
`entsoe-py` 0.8.1 (PyPI, EnergieID/entsoe-py). No ENTSO-E token is available
yet, so all entsoe-py evidence is offline-reproduced or read from its source.

## What was compared

- `EntsoePandasClient.query_imbalance_prices` (ENTSO-E A85, ZIP/XML) vs
  `nordic_balancing.EntsoeClient.imbalance_prices` (same endpoint, httpx).
  Driven by identical synthetic A85 responses — no network.
- `nordic_balancing.ESettClient` (keyless eSett `/EXP14/Prices`) vs the Nordic
  reality it exposes: the 2023-05-21 resolution switch, quarter-hour price
  repetition, and unpublished intervals. entsoe-py cannot query eSett at all.

## Results

### 1. entsoe-py issue #529 — reproduced offline (repro_issue_529.py)

`uv run --with entsoe-py==0.8.1 python examples/entsoe_py_comparison/repro_issue_529.py`

Synthetic server returns every 15-min point in `[periodStart, periodEnd)` for
SE_3, 2023-12-30 → 2025-01-03 Europe/Stockholm (35,520 expected rows). The
`year_limited` decorator issued two block queries (`202312292300→202412292300`,
`202412292300→202501022300` UTC) and returned **35,519 rows**; the missing
timestamp is `2024-12-30T00:00:00+01:00` — the first point of block 2, dropped
by the strict `index > _start` mask (decorators.py:141-145) even though the API
treats `periodEnd` as exclusive. Same server through `nordic_balancing.
EntsoeClient` (7-day chunks, httpx.MockTransport): **35,520 rows, 0 missing**.

### 2. Resolution switch 2023-05-21T22:00Z (nordic_quirks.py, live eSett)

`uv run python examples/entsoe_py_comparison/nordic_quirks.py`

SE3 and FI, one day each side: before = 96 rows all `resolution=1:00:00`
(hourly values expanded to four quarters); after = 96 rows all `0:15:00`.
Confirmed exactly.

### 3. "Repeated hourly prices" claim (same run, 20 sample weeks)

First week of every second month 2023-07 → 2026-09, all 12 zones:

| period                                   | hours  | all-4-quarters identical |
|------------------------------------------|--------|--------------------------|
| before 2025-03-18T23:00Z (19 Mar CET)    | 22,176 | **100.0 %** (every zone) |
| after                                    | 18,144 | **3.3 %** overall        |

Per-zone after-shares: 0.4–9.3 % (NO zones higher than DK/SE/FI).
The claim holds, with a corrected date. Until 2025-03-19 00:00 CET the imbalance
price was effectively hourly, even though it was published per quarter. The mFRR energy
activation market went live on 2025-03-04, but mFRR and imbalance pricing moved to
15 minutes only on 19 March
([Fingrid](https://fingrid.fi/en/news/news/2025/go-live-of-15-min-intraday-cross-border-trading-in-the-nordics-on-1803-for-delivery-on-1903)).
A first run that split at 2025-03-04 put the uniform first week of March 2025 into
"after" and reported 9.1 %. That figure is superseded. See also
`examples/mfrr_transition/` for the day-by-day view.

### 4. Unpublished intervals (same run, last ~6 h, all 12 zones)

Window 2026-10-09T07:00Z–13:00Z: every zone returned all 24 expected rows;
**zero intervals missing entirely**. Trailing unpublished intervals come back
as records with `imbalance_price_eur is None`: DK1/DK2 0 (fully published),
FI 6, each NO zone 8, each SE zone 5. These counts are a snapshot: a rerun
about 15 minutes later gave FI 2, NO 4, SE 1 and still zero missing, because
publication catches up over time.

Caller-visible behavior, read from entsoe-py 0.8.1 source:

- Inside a returned document, entsoe-py builds the series only from `<Point>`
  elements present (series_parsers.py:100-107). For `curveType A01` gaps become
  **absent rows** (no NaN, no reindex); for `A03` missing positions are
  reindexed and **forward-filled** (series_parsers.py:109-114), hiding gaps.
- If a whole year block returns "No matching data found", `year_limited`
  treats it as an empty frame (decorators.py:147-152) — a silent hole inside a
  multi-year answer; only if *all* blocks are empty does it raise
  `NoMatchingDataError` (decorators.py:155-157; detection at entsoe.py:141-143).
- nordic-balancing eSett: caller sees the interval with
  `imbalance_price_eur=None` — present but unpublished is distinguishable.
  Its strict ENTSO-E parser instead *raises* `SourceError` on a missing
  position unless `curveType A03` (src/nordic_balancing/sources/entsoe.py:340-346).

## Where entsoe-py is better or equal

- Coverage: all of Europe (~40+ areas) vs 12 Nordic zones.
- API surface: ~40 query types (day-ahead, load, generation, cross-border,
  capacities, unavailability, …) vs imbalance prices/volumes only.
- Maturity: 730 stars, years of production use, known quirks documented in
  issues; pandas DataFrame output is convenient for analysis.
- Raw client exposes untouched XML/ZIP bytes; handles A03 ffill.

## Gaps found in nordic-balancing itself

- `ImbalancePrice.resolution` labels post-2023 quarters `15 min` even when all
  four quarters of an hour carry identical values (before 2025-03-19 the
  values were effectively hourly). The label records publication cadence, not information
  content — a caller cannot distinguish "real" 15-min pricing.
- `EntsoeClient` is mock-tested only; its A85 assumptions (single price, EUR,
  A01/A03, strict positions) may raise `SourceError` on real documents entsoe-py
  tolerates, e.g. dual-price TimeSeries.
- eSett history only starts 2021-10-31T23:00Z.
- `None` conflates "not yet published" and "not provided by this source".

## What still needs the ENTSO-E token

- Any live entsoe-py call (every query needs a token) — including a live
  verification of #529 and whether ENTSO-E actually returns ZIP for A85.
- Live verification of `nordic_balancing.EntsoeClient`'s parser assumptions.
- `live_side_by_side.py` is ready: SE3 + FI, 2026-01-15, three-way diff. It
  exits with "ENTSOE_API_KEY not set" today. Command:
  `uv run --with entsoe-py==0.8.1 python examples/entsoe_py_comparison/live_side_by_side.py`

Notes: entsoe-py issues #435/#523 concern `query_procured_balancing_capacity`
(Slovakia) — a reliability signal for capacity queries, not imbalance prices.
