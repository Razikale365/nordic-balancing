# Changelog

All notable changes to this project. Versions follow [PEP 440](https://peps.python.org/pep-0440/);
the public API is defined in [docs/API_CONTRACT.md](docs/API_CONTRACT.md), and any change to it
is listed here.

## Unreleased

- Add `reconcile_reserve_capacity`, `CapacityDivergence`, `CapacityReconciliationReport` and `DivergenceKind.VOLUME` to classify reserve capacity price, volume and coverage differences between sources.

## 1.0.0rc2 — 2026-10-26

Second release candidate: fixes from a wide pre-1.0 audit.

### Security
- **ENTSO-E and Fingrid:** a response that echoes the API key is rejected in any letter case and in any form the JSON or XML decoder turns back into the key (character references, `\uXXXX` and `\/` escapes, UTF-16/32 bodies, a key split by XML comments, CDATA or processing instructions), not only as raw or percent-encoded bytes.
- **ENTSO-E:** each ZIP member is read in bounded steps, so a member that lies about its size can no longer make the library inflate it in full before rejecting it.

### Changed
- `changes_between` no longer reports a change taking effect exactly at `end`: no interval of the `[start, end)` window is under it.
- JSON with a repeated member name, or nested more than 32 levels deep, raises `SourceError` after the usual retries instead of silently keeping the last value or failing with `RecursionError`.
- **ENTSO-E:** equal imbalance prices merge only across the `A04`/`A05` category pair; any other category raises `SourceError`.
- A `start` or `end` with nanoseconds (a pandas `Timestamp`) has them dropped instead of being rejected or losing an interval.

### Fixed
- **eSett:** a query whose `start` had sub-second digits lost the interval at each 31-day chunk boundary; chunk boundaries now sit on the quarter hour.
- A response that a caller's transport read before returning it is still checked against the 64 MiB limit and for an unrequested encoding.
- **ENTSO-E:** an XML declaration in UTF-16 or naming an unknown encoding raises `SourceError` instead of `ValueError` or `LookupError`.
- The release workflow checks the distributions with a lockfile-pinned `twine`.

## 1.0.0rc1 — 2026-10-22

Release candidate for 1.0: 0.4.0 plus the fixes below. Install it with
`pip install --pre nordic-balancing` or `pip install nordic-balancing==1.0.0rc1`.

### Compatibility promise
From 1.0.0 the public API defined in [docs/API_CONTRACT.md](docs/API_CONTRACT.md) follows Semantic
Versioning: no breaking change before 2.0, deprecations warn for at least one minor release first,
and a provider change that breaks parsing of valid data is fixed in a bug-fix release. The contract's
"Compatibility and deprecation policy" section has the full rules.

### Security
- **ENTSO-E:** the API token is sent in a request header instead of the URL. Until now `httpx` logged it with every request URL at `INFO`, so an application with `logging.basicConfig(level=logging.INFO)` wrote the token to its logs. Rotate your ENTSO-E token if such logs may have been kept or shared.
- **ENTSO-E and Fingrid:** keys are better protected from a hostile or misbehaving server and from misconfiguration. A response that echoes the key is rejected instead of being returned in `raw`; provider text that might hide the key in an encoded form is withheld from error messages; errors no longer keep the original exception (which could hold the key) as their context; non-`httpx` errors from a caller's transport are wrapped by class name; and `base_url` must use `https` and must not contain a user name or password.
- Hostile responses are bounded: Fingrid and Energi Data Service FCR paging stop with `SourceError` once a response claims more rows than the query can return; ENTSO-E XML with a DTD is rejected; a ZIP response with too many entries is rejected before its directory is parsed; provider text in error messages has control characters escaped and long values cut.

### Added
- Python 3.11 is supported.
- `CHANGES` records Finland's switch to a volume-weighted imbalance price on 2026-06-01 (Finnish time), from Fingrid's announcement and confirmed in its published prices.

### Fixed
- **Fingrid:** a `FingridClient` shared across threads keeps its minimum spacing between requests; before, waiting threads sent their requests at the same moment.
- **ENTSO-E:** the API token is redacted from error messages in any percent-encoding (lowercase hex, or `/` left unescaped), not only the two most common ones. Keys in ENTSO-E's usual UUID format need no encoding and were not affected.
- The 64 MiB response limit is enforced while the body is read, so an oversized, endless or highly compressed response stops early instead of being held in memory first.
- Compressed responses: every member of a multi-member gzip body is read (not only the first), bytes after the end of a compressed stream no longer stall the read, a gzip body cut short is retried like other undecodable bodies, and a body in an encoding that was not requested (such as `br`) raises `SourceError` instead of being decoded without a size bound. Error statuses in such an encoding are still retried and reported by status.
- **ENTSO-E:** an imbalance price published once without a category and again with one is rejected as a duplicate, instead of being merged as if it were a second category.
- **ENTSO-E:** a reserve-capacity Period far longer than the request, a ZIP response with more than 10,000 members, and an HTTP 400 that carries data instead of an acknowledgement raise `SourceError` instead of being processed.
- **eSett:** a DK1/DK2 row with all prices `0.0` and a null direction is reported as unsettled (`None`), like the same row with direction `0`.
- **Svenska kraftnät:** a result total larger than the query can match raises `SourceError` before any further pages are fetched.
- `reconcile_imbalance_prices` reports a NaN or infinite price against an unpublished one as a PRICE divergence, as documented.
- `changes_between` raises `ValueError`, not `TypeError`, for an unhashable `zones` member.

## 0.4.0 — 2026-10-20

Correctness fixes and faster parsing. Re-fetch Danish eSett data fetched with 0.3.1 or earlier.

### Changed
- Sources fail loudly on more provider changes: SvK fetches quarter-hour start times, so a switch to 15-minute data raises instead of repeating the hourly value (about four times as many requests for hourly data); eSett and Energi Data Service raise on hour-only imbalance rows where 15-minute data is expected; Energi Data Service FCR paging stops on a repeated page.
- JSON responses are capped at 64 MiB, like ENTSO-E XML.
- Parsing is faster with identical output: eSett about twice as fast, ENTSO-E and Energi Data Service 23-57 % (see `benchmarks/`).

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
