# Changelog

All notable changes to this project. Versions follow [PEP 440](https://peps.python.org/pep-0440/);
the project is pre-alpha and the API may change between alphas.

## Unreleased

Second audit, against the published 0.1.0a3. No public names are removed or renamed.

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
- Verification: 412 offline tests pass. Live: 8 passed, and the 2 ENTSO-E tests were
  skipped (no token).

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
