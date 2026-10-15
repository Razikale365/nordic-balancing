# Changelog

All notable changes to this project. Versions follow [PEP 440](https://peps.python.org/pep-0440/);
the project is alpha and the API may change between 0.x versions.

## Unreleased

### Fixed
- **Fingrid:** the API key has surrounding whitespace stripped; an empty key, or one with
  internal whitespace or characters outside printable ASCII, raises `ValueError` at
  construction without echoing it. Before, such a key could reach an uncaught
  `UnicodeEncodeError` that carried the key.
- **Fingrid:** an error response's body is no longer decoded, so a 429/502/503/504 with a
  broken compressed body is retried as its status says. Before, it raised a decoding error and
  skipped the retry rules.
- **Fingrid:** the query start is floored to the hour only for the hourly datasets (319, 244,
  106, 369); other datasets floor to the quarter, so a mid-hour start no longer fetches or
  returns an earlier record.
- **Fingrid, SvK (pagination):** an empty or short page before the reported total, or rows
  beyond it, raise `SourceError` at that page, as README contract C2 states. Before, Fingrid
  requested every page before checking, and SvK accepted a short page.
- **Energi Data Service, SvK (pagination):** the total from the first page is kept; a later
  page reporting a different total raises. Before, a smaller later total turned an incomplete
  fetch into success.
- **Energi Data Service, SvK (timestamps):** a date without a time of day, a non-UTC offset, or
  a value at the datetime limits raises `SourceError`. Naive timestamps (what both providers
  send) and explicit UTC designators (`Z`, `+00:00`) are read as before.
- **All clients (HTTP):** a redirect, an informational status or HTTP 204 now raises
  `SourceError` naming the status (for example `HTTP 302`), without the response body. Before,
  such a response was returned as data, or reported as "not JSON". Redirects are still never
  followed.
- **All clients (`Retry-After`):** an HTTP-date with a numeric offset is converted using that
  offset, and one with an unrecognised zone is treated as malformed (the default 10 s wait).
  Before, an unrecognised zone was read as UTC, so the wait could be off by hours.
- **ENTSO-E (secrets):** the token is now redacted before a server message is truncated,
  so a token straddling the cut can no longer leak a prefix. The API key has surrounding
  whitespace stripped, and a key with internal whitespace or characters outside printable
  ASCII raises `ValueError` without echoing it.
- **ENTSO-E (records):** a PT60M point that does not start on the hour, the same position
  twice within one Period, and the same series key in two chunk results now raise
  `SourceError`. Before, an off-hour point could expand past the request window, a repeated
  position could hide a missing category or a price conflict, and the chunk merge silently
  kept one value.
- **ENTSO-E (documents):** a corrupt or empty ZIP archive, an XML encoding that cannot be
  decoded, a present but empty code element (document type, area, category, direction) and a
  missing document type now raise `SourceError`. Before, some raised `ValueError`, `OSError`,
  `LZMAError` or `LookupError`, and others were accepted or returned no records silently.
- **ENTSO-E (acknowledgements):** an acknowledgement means "no data" only when every reason
  says so; any other reason raises. Before, one "No matching data found" reason hid the others.
- **Records:** a `start` in a non-UTC time zone at offset zero (for example
  `Europe/London` in winter) is now converted to UTC. Before, `end` was computed in
  local wall time and could fall before `start` at the autumn clock change.
- **ENTSO-E:** implied A03 positions now stop at the request window end, with filling
  work bounded by that window. Before, a Period extending beyond a chunk could
  fill intervals belonging to the next chunk, even when that chunk returned no data.
- **Energi Data Service:** a record that is not a JSON object (for example a number or `null`
  in `records`) now raises `SourceError`, as the data contracts require. Before, it
  raised `AttributeError`.

## 0.1.1 — 2026-10-12

Corrections from an independent audit of 0.1.0. No public names are added, removed or
renamed.

### Fixed
- **ENTSO-E:** the same price category published twice for one interval (or two
  records without a category) now raises `SourceError`, as contract C3 requires for
  every other duplicate. Before, an equal price collapsed silently and `raw["categories"]`
  listed the category twice. Equal prices under distinct categories (A04 and A05)
  still collapse.
- **Retry-After (all clients):** an HTTP-date (RFC 9110) is now honoured as the time
  to wait, and a past date retries at once. Before, it was ignored: the client waited
  the 10 s default, and a date more than 300 s away did not raise `RateLimitError`. Delay-seconds handling is unchanged.

### Added
- `examples/entsoe_validation/validate.py`: an opt-in (`--live`) comparison of
  ENTSO-E with eSett over the fixed sample windows and the full year 2025. Every
  interval lands in one category (missing, unpublished, exact, rounding, zero price,
  material difference); results are written as JSON. Its classification is tested
  offline in `tests/test_entsoe_validation.py`.

### Documentation
- README C3 states both exceptions precisely: distinct ENTSO-E categories, and the
  Fingrid hourly/15-minute overlap rule. The ENTSO-E row gives the FI start
  (2021-10-31T22:00Z). `ReconciliationReport.identical` lists what it compares.
- Request timings are described as observed latency, not server time.
- `docs/RELEASE_EVIDENCE.md`: dated header; the sample is 60 zone-windows (72
  zone-days), not 60 zone-days; the full-year 2025 comparison is restated from a rerun
  of the committed script, including 1 DK1 material difference the 0.1.0 text left
  out. That DK1 value (2141.93 vs eSett 59.70) is also Energi Data Service's imbalance
  price and equals the aFRR up price; the providers disagree, the client does not
  misparse it.
- The 0.1.0 entry now says 60 zone-windows and mentions the 4 larger full-year
  differences.

### Behaviour changes to note
- An ENTSO-E response that repeats a price category for one interval now raises. No
  observed live document did.

## 0.1.0 — 2026-10-12

First release that is not a pre-release: `pip install nordic-balancing` no longer needs
`--pre`. All five sources are now checked against their live APIs. The classifier moves
from Pre-Alpha to Alpha; the API may still change between 0.x versions.

Also contains the second audit, against the published 0.1.0a3. No public names are
removed or renamed.

### Changed
- **ENTSO-E is faster.** Observed request latency (2–42 s on 2026-10-12) did not
  grow with the window, so `EntsoeClient` now asks for up to 366 days per request
  (was 7) and runs up to `max_workers` requests in parallel (new keyword, default 12; `max_workers=1` runs
  them in turn). A year of all 12 zones takes about 90 s instead of 624 requests. The
  default HTTP timeout rises from 30 s to 120 s, since single requests took up to 42 s.

### Added
- `to_frame(records)` returns a pandas DataFrame with one typed column per field, leaving
  out `raw`. pandas is an optional extra (`pip install "nordic-balancing[pandas]"`) and is
  imported only when `to_frame` is called. Tested with pandas 2.1.4, 2.2.3 and 3.0.6.

### Documentation
- README: the status line now says releases are on PyPI, and there is an install
  section. "Planned sources" became "Sources", with a column saying which are checked
  against the live API (now all five). The Energi Data Service mFRR/aFRR datasets
  are listed as not covered yet: they were never implemented.

### Fixed
- **ENTSO-E:** a document whose area (`area_Domain.mRID`, or `controlArea_Domain.mRID`
  before schema 4.1) differs from the requested zone's EIC now raises. Before, its
  prices were labelled with the requested zone.
- **ENTSO-E:** the same interval published at two resolutions (PT15M and PT60M) now
  raises, even when the prices are equal. Before, the two were merged silently and the
  first resolution was kept.
- **ENTSO-E:** the price unit is now checked in schema versions before 4.5 too, where
  the element is `price_Measure_Unit.name` (4.5 renamed it `price_Measurement_Unit.name`).
  Before, a non-MWH unit in those versions was accepted.
- **ENTSO-E:** a balancing document whose `type` is not A85 (for example A86, imbalance
  volumes) now raises instead of yielding only unpublished intervals.
- **ENTSO-E:** a Period that is not a whole number of resolution steps now raises.
  Before, a partial last step was expanded past the Period end.
- **ENTSO-E:** a corrupt ZIP member raises `SourceError` instead of a raw `zlib.error`.
- **Energi Data Service:** `TimeUTC` with fractional seconds (`00:00:00.5`) is rejected.
  Before, the boundary check truncated it and returned an off-grid interval start.
- **Energi Data Service:** `max_retries` is validated like the other clients. A
  negative value used to fail later with `UnboundLocalError`.
- **Fingrid:** a 60-minute record must start on the hour. Two off-hour hourly records
  could overlap, and expansion silently kept one of them.
- **Fingrid:** the API key is never sent to a redirect target, even when the supplied
  `httpx.Client` follows redirects.
- **Retries (all clients):**
  - HTTP 502 and 504 and transient transport failures (timeouts, network errors,
    dropped connections) are retried within the same `max_retries` budget. After the
    last retry they raise `SourceError`, not `RateLimitError`.
  - A non-finite `Retry-After` (`inf`, `nan`) falls back to the default delay; it used
    to crash `time.sleep`.
  - A `Retry-After` above 300 s raises `RateLimitError` at once instead of sleeping
    through it.

### Tests
- Regression tests for every fix above, each confirmed to fail on 0.1.0a3. Guard tests
  for inputs that stay valid pass on both versions.
- `tests/test_properties.py` (Hypothesis, a dev-only dependency) adds property tests:
  for every client, splitting a window gives the same records, and every start is on the
  15-minute grid in `[start, end)` with none missing. It also checks reconciliation
  symmetry.
- Verification at the end of the second audit, before ENTSO-E was live-verified:
  421 offline tests pass. Live: 8 passed, and the 2 ENTSO-E tests were skipped (no
  token). The final 0.1.0 counts are below.
- **ENTSO-E is now live-verified** (2026-10-12). The 2 live tests pass, and a third,
  `test_live_matches_esett_prices_but_not_its_resolution_label`, pins equal SE3 prices
  with differing resolution labels against eSett. A probe of all 12 zones over 60
  zone-windows (72 zone-days) matched eSett on every interval (DK1/DK2: a few 0.01 EUR
  rounding differences), and SE3/FI matched entsoe-py 0.8.1 exactly. No parser check fired.
  Details and the per-zone `PT15M` dates are in `docs/RELEASE_EVIDENCE.md`.
- A full year (2025, all 12 zones) against eSett found gaps and zero prices in ENTSO-E's
  own documents (mostly DK, January and April 2025), confirmed with entsoe-py, and 4
  larger price differences; they are listed in `docs/RELEASE_EVIDENCE.md`. Final count: 427 offline tests and 11 live tests
  pass.

### Behaviour changes to note
- Some ENTSO-E responses that were accepted before now raise: a mismatched area, a
  resolution conflict, a non-A85 document type, a non-MWH unit before schema 4.5, or a
  partial Period.
- A call that hits HTTP 502/504 or a timeout now takes longer before it fails, because
  of the retries (10 s per retry by default).

## 0.1.0a3 — 2026-10-09

Data-integrity fixes from an audit. The public API only gains fields; nothing is removed
or renamed.

### Fixed
- **Energi Data Service:** `DominatingDirection` must be exactly -1, 0 or 1. Previously
  `int()` silently truncated fractional values, so 0.6 became `NONE`. Real data contains
  only -1, 0, 1 or null.
- **Fingrid:** hourly expansion and the 2025-03 hourly/15-minute overlap rule now apply
  only to datasets 319, 244, 106 and 369 (imbalance price, mFRR up/down price,
  dominating direction). For any other dataset, `series()` rejects 60-minute records
  with `SourceError` instead of repeating values that may be additive totals.
- **`raw` provenance:** each record now holds its own deep copy. Before, nested data was
  shared with the caller's source record and between the four quarters of an expanded
  hourly record, so mutating one changed the others. The top level stays read-only.
- **eSett:** a price published in only one of the sales/purchase columns is used as the
  single price instead of becoming `None`. This has never been observed: in ~107k
  sampled records both columns are always present and equal, or both null. Unequal
  prices still raise.

### Added
- `ReconciliationReport.primary_only_zones` and `.reference_only_zones` list the zones
  present in only one input, so coverage gaps are no longer silent. Both default to
  `()`.

### Behaviour changes to note
- `FingridClient.series()` on a dataset other than 319/244/106/369 now raises if the
  source returns hourly records.
- A non-integer Energi Data Service direction now raises instead of being truncated.

### Verification
- Ruff, mypy and 370 offline tests pass.
- Live tests: 8 passed. The 2 ENTSO-E live tests were skipped because no token is
  available. ENTSO-E is checked against the official schema but is still not
  live-verified.

## 0.1.0a2 — 2026-10-09

### Fixed
- Cap `httpx` below 1.0. With 0.1.0a1, `pip install --pre` resolved an incompatible
  httpx 1.0 development build, and the import failed.

### Added
- CI and the release workflow install the built wheel in a clean environment, with
  pre-release dependencies allowed, and import it.

## 0.1.0a1 — 2026-10-09

First alpha.
- Clients:
  - Energi Data Service (DK1/DK2);
  - eSett (all 12 zones);
  - Fingrid (FI, including hourly history until 2025-03-18);
  - Svenska kraftnät (SE1–SE4 mFRR/aFRR capacity markets);
  - ENTSO-E A85 (schema-checked, not live-verified).
- Every series is on 15-minute UTC intervals, under shared data contracts C1–C8 (see
  README).
- Dated market changes (`changes_between`) and cross-provider reconciliation
  (`reconcile_imbalance_prices`).
