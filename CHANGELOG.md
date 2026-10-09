# Changelog

All notable changes to this project. Versions follow [PEP 440](https://peps.python.org/pep-0440/);
the project is pre-alpha and the API may change between alphas.

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
