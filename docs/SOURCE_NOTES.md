# Source notes

What each source's API actually does, as observed against the live services. These are the
quirks the clients handle and the reason for most of the validation in the code. Each item
carries the date it was observed and points to the file, test or script that shows it.
The public contract is in [API_CONTRACT.md](API_CONTRACT.md); per-source fields are in
[FIELD_MATRIX.md](FIELD_MATRIX.md).

## History starts

| Source | Earliest data |
|---|---|
| Energi Data Service | imbalance 2025-03-04; aFRR 2022-12-07T23:00Z; mFRR 2023-06-20T22:00Z; FcrDK1 2021-01-18T23:00Z; FcrNdDK2 2021-11-09T23:00Z |
| Fingrid | hourly publication until 2025-03-18; 15-minute records from 2025-03-14T23:15Z |
| Svenska kraftnät | hourly records; earliest hour not verified |
| eSett | 2021-10-31T23:00Z |
| ENTSO-E | imbalance 2021-10-31T23:00Z (FI 22:00Z); NO FCR-N 2014-09-30T22:00Z; NO mFRR 2024-02-11T23:00Z; FI mFRR 2024-11-12T23:00Z |

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
  rerun 2026-10-12 with the same counts as the first, uncommitted probe; the run writes
  per-zone counts and every differing interval to `results_year-2025.json`):
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
  ones raise; a point without a category beside one with a category is a duplicate.
  Acknowledgement documents, ZIP and plain XML bodies are handled, with a 64 MiB cap
  (enforced while reading, and on the ZIP members' total size) and at most 10,000 ZIP
  members. An HTTP 400 is accepted only when it carries an acknowledgement.
- Further checks taken from the XSDs:
  - The document-level area is `controlArea_Domain.mRID` in v3.0–v4.0 and
    `area_Domain.mRID` from v4.1. It is optional in all versions, and when present it
    must equal the requested EIC.
  - The price unit is `price_Measure_Unit.name` up to v4.4 and
    `price_Measurement_Unit.name` in v4.5. Both are checked.
  - `type` is mandatory and must be A85; a document without it raises.
  - A Period must be a whole number of resolution steps.
  - An interval published at two resolutions raises, even with equal prices.
- ENTSO-E live tests, including capacity checks and the Fingrid cross-check, skip without
  `ENTSOE_API_KEY`; the cross-check also requires `FINGRID_API_KEY`.

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


## Memory

One year of all 12 zones (420,480 records) peaks at about 1.3 GiB resident for eSett and
2.3 GiB for ENTSO-E, measured offline (`examples/benchmarks/`). Most of the ENTSO-E excess
is the XML parser working on up to 12 parallel requests and is released when the call
returns; the records themselves stay under 3 KiB each. To use less, fetch fewer zones or a
shorter window per call, or lower `max_workers` on `EntsoeClient`.
