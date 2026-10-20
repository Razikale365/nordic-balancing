# Changelog

All notable changes to this project. Versions follow [PEP 440](https://peps.python.org/pep-0440/);
the project is in beta: the public API is defined in
[docs/API_CONTRACT.md](docs/API_CONTRACT.md), and any change to it before 1.0 is listed here.

## Unreleased

### Changed
- Sources fail loudly on more provider changes: SvK fetches quarter-hour start times, so a switch to 15-minute data raises instead of repeating the hourly value (about four times as many requests for hourly data); eSett and Energi Data Service raise on hour-only imbalance rows where 15-minute data is expected; Energi Data Service FCR paging stops on a repeated page.
- JSON responses are capped at 64 MiB, like ENTSO-E XML.

### Fixed
- **eSett:** unsettled DK1/DK2 intervals (all prices `0.0`) are returned as unpublished (`None`); re-fetch DK data fetched with 0.3.1 or earlier.
- **Fingrid:** a window with no rows returns `[]` instead of raising `SourceError`.
- **ENTSO-E:** the API token is redacted from percent-encoded error messages too.
- Records and reports can be deep-copied, pickled and passed to `dataclasses.asdict`.
- `reconcile_imbalance_prices` never counts a NaN or infinite price as identical.
- `changes_between` raises `ValueError` for an empty `zones` or one that is not an iterable of `BiddingZone`.
- A `start`/`end` that is not a `datetime` raises `TypeError`; one outside the years 2-9997 raises `ValueError`.
- A 200 response that is not valid JSON is retried like other undecodable bodies.

## 0.3.1 — 2026-10-18

### Changed
- Building records is faster: one-year queries run 19-36 % quicker with identical output (see `benchmarks/`).

### Fixed
- **Energi Data Service:** FCR-N and FCR-D `capacity_market` no longer raises `SourceError` on the day the clocks go back in October (for example 2025-10-26); the hours are mapped to UTC as ENTSO-E and SvK do.

## 0.3.0 — 2026-10-17

First published beta, and the reserve-capacity release. 0.2.0 was never published; its changes ship here, so when upgrading from 0.1.1 read both sections.

### Added
- **Reserve capacity (`capacity_market`)** from Energi Data Service, Fingrid and ENTSO-E, returning `ReserveCapacity` records. The public API only grows.
- **Energi Data Service:** `capacity_market(product, start, end, *, zones=(DK1, DK2))` for aFRR (all 12 zones), mFRR (DK1, DK2), FCR-N and FCR-D (DK2, SE1-SE4) and FCR (DK1). FCR products need explicit `zones`.
- **Fingrid:** `capacity_market(product, start, end)` for FI hourly-market FCR-N, FCR-D and mFRR.
- **ENTSO-E:** `capacity_market` for FCR-N (Norwegian zones) and mFRR (NO1-NO5 and FI).
- New `ReserveProduct` members `FCR_N`, `FCR_D` and `FCR`. FCR-N and FCR are symmetric and use `Direction.NONE`: count such a record in both directions when summing volume, once when summing cost.
- README names the CC BY 4.0 data sources; `docs/API_CONTRACT.md` states the deprecation policy and known limitations.

### Changed
- **Breaking:** an eSett or Svenska kraftnät record that lacks a value key (a null value is still `None`) and any HTTP status other than 200 on a data request now raise `SourceError`.
- The ENTSO-E request-failure message ends with the exception class (`entsoe: request failed (ConnectError)`); code comparing the whole message must allow for it.
- More malformed input raises `SourceError` instead of being accepted: duplicate or non-ASCII numeric fields and bad namespaces in ENTSO-E; missing columns and repeated auction rows in Energi Data Service; non-UTC timestamp designators in all sources; over-nested JSON.
- A `base_url` that is not an absolute http or https URL raises `ValueError` at construction.
- Malformed `Retry-After` values (`5_0`, `+120`, `-3`, `1e400`) use the default 10-second delay.
- `changes_between(start, end)` raises `ValueError` when `end` is before `start` (it returned an empty list).

### Fixed
- **Energi Data Service:** queries over 10,000 rows were silently truncated to the first page. 0.1.1 is affected: re-fetch any imbalance-price query that returned exactly 10,000 records (both Danish zones, any window longer than about 52 days).
- **Energi Data Service:** FCR-N and FCR-D requests fetch only the requested product.
- **ENTSO-E:** long FCR-N and mFRR queries no longer fail on the 100-instance limit; identical repeated series collapse to one record set, while differing values still raise.
- **Svenska kraftnät:** `capacity_market` with an unpublished product raises `ValueError` instead of leaking a `KeyError`.
- **ENTSO-E:** errors raised while sending a request never expose the token.
- **HTTP:** an undecodable response body is retried within `max_retries`.
- **Records:** `raw` deep-copies any non-mapping, non-list value, so it is never shared with caller input.

## 0.2.0 — not published (included in 0.3.0)

First beta. The public surface is written down and tested, and every source is checked against its live API daily. No public signature or record field changed.

### Added
- `docs/API_CONTRACT.md` (public API, compatibility policy from 1.0) and `docs/FIELD_MATRIX.md` (fields each source provides); tests fail on any change to either.
- Python 3.14 support and a CI job at the lowest declared dependency versions.
- Daily live tests, property tests, `examples/benchmarks/`, `SECURITY.md` and `CONTRIBUTING.md`.

### Changed
- Development status is now `4 - Beta`.
- `FingridClient` and `EntsoeClient` raise `ValueError` at construction for an API key with internal whitespace or non-printable characters.
- Stricter checks: data that 0.1.1 accepted now raises `SourceError` (ENTSO-E documents and records, pagination totals, timestamps, non-success HTTP statuses).

### Fixed
- **Fingrid:** an invalid API key never appears in an error; error responses with a broken body are retried; the query start floors to the hour only for datasets 319, 244, 106 and 369.
- **Fingrid, SvK:** short or extra pages and changing totals raise `SourceError`.
- **Energi Data Service, SvK:** a date without a time of day or a non-UTC offset raises; SvK accepts `Z` and `+00:00`.
- **All clients:** a redirect, informational status or HTTP 204 raises `SourceError`; an HTTP-date `Retry-After` with an offset or unknown zone is read correctly.
- **ENTSO-E:** the token is redacted before truncation; off-hour points, repeated positions and conflicting chunks raise; corrupt ZIPs and bad encodings raise `SourceError`; an acknowledgement means "no data" only if every reason says so.
- **Records:** a `start` in a non-UTC zone at offset zero is converted to UTC.

## 0.1.1 — 2026-10-12

Corrections from an independent audit of 0.1.0. No public names changed.

### Added
- `examples/entsoe_validation/validate.py`: an opt-in (`--live`) comparison of ENTSO-E with eSett.

### Changed
- An ENTSO-E response that repeats a price category for one interval now raises `SourceError`.

### Fixed
- **All clients:** a `Retry-After` HTTP-date is honoured, and a date more than 300 s away raises `RateLimitError`.
- README contract C3 states both exceptions precisely.

## 0.1.0 — 2026-10-12

First release that is not a pre-release: `pip install nordic-balancing` no longer needs `--pre`. All five sources are checked against their live APIs.

### Added
- `to_frame(records)` returns a pandas DataFrame; pandas is an optional extra (`pip install "nordic-balancing[pandas]"`).
- README install section and a Sources table.

### Changed
- **ENTSO-E:** faster. Requests cover up to 366 days (was 7) and run in parallel (`max_workers`, default 12); the default HTTP timeout is 120 s (was 30 s).
- **Breaking:** ENTSO-E responses with a mismatched area, resolution conflict, non-A85 document type, non-MWH unit before schema 4.5, or partial Period now raise.
- Calls that hit HTTP 502/504 or a timeout take longer to fail, because they are retried.

### Fixed
- **ENTSO-E:** the same interval at two resolutions raises; the price unit is checked before schema 4.5; a corrupt ZIP member raises `SourceError`.
- **Energi Data Service:** fractional-second `TimeUTC` is rejected; negative `max_retries` is validated.
- **Fingrid:** a 60-minute record must start on the hour; the API key is never sent to a redirect target.
- **All clients:** HTTP 502/504 and transient transport failures are retried within `max_retries`; a non-finite `Retry-After` uses the default delay; one above 300 s raises `RateLimitError` at once.

## 0.1.0a3 — 2026-10-09

### Added
- `ReconciliationReport.primary_only_zones` and `.reference_only_zones` list zones present in only one input.

### Changed
- **Breaking:** `FingridClient.series()` on a dataset other than 319/244/106/369 raises if the source returns hourly records, and a non-integer Energi Data Service direction raises instead of being truncated.

### Fixed
- **`raw` provenance:** each record holds its own deep copy; nested data was shared with the caller and between expanded quarters.
- **eSett:** a price in only one of the sales/purchase columns is used as the single price.

## 0.1.0a2 — 2026-10-09

### Fixed
- Cap `httpx` below 1.0; `pip install --pre` resolved an incompatible development build.

## 0.1.0a1 — 2026-10-09

First alpha: clients for Energi Data Service, eSett, Fingrid, Svenska kraftnät and ENTSO-E; 15-minute UTC series under shared data contracts (see docs/API_CONTRACT.md); `changes_between` and `reconcile_imbalance_prices`.
