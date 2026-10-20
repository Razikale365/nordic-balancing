# Changelog

All notable changes to this project. Versions follow [PEP 440](https://peps.python.org/pep-0440/);
the project is in beta: the public API is defined in
[docs/API_CONTRACT.md](docs/API_CONTRACT.md), and any change to it before 1.0 is listed here.

## Unreleased

Fixes from the release-candidate audit.

### Fixed
- **eSett:** a DK1/DK2 interval that is not settled yet is no longer returned as a real
  price. eSett publishes it with every price at `0.0` and no regulation direction; it is
  now reported as unpublished (`None` prices and direction), like the null rows of the
  other zones, and `reconcile_imbalance_prices` classifies it as UNPUBLISHED. Records
  fetched with 0.3.1 or earlier for the latest DK hours may carry these `0.0` prices;
  re-fetch them.
- **Fingrid:** a window with no rows returns `[]` instead of raising `SourceError`
  ("page 1 has invalid lastPage"); paging follows the page size the server reports.
- **ENTSO-E:** the API token is redacted from error messages in its percent-encoded
  forms too.
- Records and reports can be deep-copied, pickled and passed to `dataclasses.asdict`
  (`raw` is still a read-only mapping).
- `reconcile_imbalance_prices` never counts a NaN or infinite price as identical.
- `changes_between` raises `ValueError` for a `zones` argument that is not an iterable of
  `BiddingZone` values (a bare zone used to return `[]`) or is empty.
- A `start`/`end` that is not a `datetime` raises `TypeError`; one outside the years
  2–9997 raises `ValueError` instead of an `OverflowError`.
- A 200 response that is not valid JSON is retried like other undecodable bodies, as
  documented.

### Changed
- Sources now fail loudly on more provider changes: SvK fetches quarter-hour start times
  so a switch to 15-minute data raises instead of repeating the hourly value (about four
  times as many requests for hourly data); eSett and Energi Data Service raise if
  imbalance rows arrive on the hour only where 15-minute data is expected; Energi Data
  Service FCR paging stops on a repeated page and raises on an unrequested product.
- JSON responses are capped at 64 MiB, like ENTSO-E XML.

## 0.3.1 — 2026-10-18

### Fixed
- **Energi Data Service:** FCR-N and FCR-D `capacity_market` no longer raises
  `SourceError` ("HourUTC '...T01:01:00' is not on a 15-minute boundary") for the day
  the clocks go back in October (2022-10-30, 2023-10-29, 2024-10-27, 2025-10-26, and
  2026-10-25 onwards). On that day `FcrNdDK2` has no 00:00 UTC hour; it publishes the
  two 02:00 Danish hours as HourUTC 01:00 (HourDK 02:00) and 01:01 (HourDK 02:01).
  The 01:00 row is now returned as 00:00 UTC and the 01:01 row as 01:00 UTC, matching
  ENTSO-E A81 (UTC) and SvK Mimer for SE3 and DK2 on 2025-10-26. `raw` keeps the
  published `HourUTC`. Only that exact pattern is mapped; any other off-boundary time
  still raises, and so does a 01:01 row on a day that also has a 00:00 row. A request
  that touches 00:00–02:00 UTC on that day now always queries both hours.

### Performance
- Building records is faster: the per-record copy of `raw` no longer dispatches through
  `copy.deepcopy` for every JSON value. On replayed one-year queries, end-to-end time fell by
  19–36 % (eSett, ENTSO-E and Energi Data Service imbalance prices), with identical output and
  unchanged peak memory. `raw` is still a deep copy per record. Method, results and the
  competitor comparison are in `benchmarks/`.

## 0.3.0 — 2026-10-17

First published beta, and the reserve-capacity release. 0.2.0 was never published: its changes
are listed in the 0.2.0 section below and ship in this version, so when upgrading from 0.1.1
read both sections. New: reserve capacity (`capacity_market`) from Energi Data Service (aFRR for
all twelve Nordic zones, mFRR and FCR for Denmark, FCR-N and FCR-D for DK2 and Sweden), Fingrid
(FCR-N, FCR-D and mFRR) and ENTSO-E (FCR-N and mFRR for Norway, mFRR for Finland). The public API
only grows: no existing signature or record field changed. Before release the code was audited
twice; the audits found no High defect in new code, but a live history check found one in all
earlier versions: Energi Data Service queries over 10,000 rows were silently truncated (see Fixed).

### Added
- **Fingrid:** `capacity_market(product, start, end)` returns FI hourly-market FCR-N,
  FCR-D and mFRR prices/procured MW as `ReserveCapacity`. Hourly values expand to
  quarters; missing price or volume rows remain `None`. Yearly-market FCR is excluded.
- **ENTSO-E:** `capacity_market` accepts mFRR (A81/B95/A47) for NO1-NO5 from
  2024-02-11T23:00Z (delivery day 2024-02-12) and FI from 2024-11-12T23:00Z
  (delivery day 2024-11-13), retaining its Norwegian default. Flow A01/A02 maps
  to UP/DOWN. Identical mFRR quantity/price repeats across documents, ZIP members
  or TimeSeries collapse by `(zone, start, direction)` after A03 filling;
  conflicting repeats and repeated positions within a Period raise. This includes
  early historical documents with identical series pairs. Identical-repeat collapse
  also applies to FCR-N; differing values still raise with its existing error text.
  For mFRR only, a one-Point position-1
  quantity-0/price-0 placeholder Period is ignored only when another non-placeholder
  TimeSeries in the same response covers the same zone, direction and exact Period.
  Lone zero series are retained; nonzero one-Point series and malformed series
  remain subject to the existing validation and conflict rules. This was observed
  on delivery day 2025-06-05 (starting 2025-06-04T22:00Z) for NO1-NO5 and FI:
  a live FI cross-check matched Fingrid on 24/24 hours for the real series and
  0/24 for the placeholders. Live
  evidence matched all 48 hour-directions against Fingrid FI and SvK SE3 on 2026-10-08.
- **Energi Data Service:** `EnergiDataServiceClient.capacity_market(product, start, end, *,
  zones=(DK1, DK2))` returns `ReserveCapacity` records: aFRR for all 12 Nordic zones (from
  2022-12-07T23:00Z) and mFRR for DK1 and DK2 (from 2023-06-20T22:00Z). Each hourly source row
  gives an up and a down record with the marginal price in EUR per MW and the procured MW,
  repeated over four quarters. Demand, DKK prices and Danish time stay in `raw`. On SE3 aFRR
  hours that both publish, the values equal `SvKClient.capacity_market`
  (`examples/eds_capacity/`).
- **FCR capacity.** New `ReserveProduct` members `FCR_N` ("FCR-N"), `FCR_D` ("FCR-D") and `FCR`
  ("FCR"). FCR-N and FCR are symmetric: their records use `Direction.NONE`, meaning the MW are
  available in both directions and paid once. Count such a record in both directions when summing
  volume per direction, and once when summing cost. FCR-D records are `UP` or `DOWN`.
  - `EnergiDataServiceClient.capacity_market` accepts the three products. FCR-N and FCR-D cover
    DK2 and SE1-SE4 from 2021-11-09T23:00Z, from Energinet's `FcrNdDK2`. Each hour gives one
    record from the source's `Total` row, with the area's procured MW and the price in EUR per MW;
    the separate auction rows stay in `raw["auctions"]`. FCR covers DK1 from 2021-01-18T23:00Z (continental FCR, from
    `FcrDK1`), with the MW procured in Denmark at the Danish settlement price. These products need
    explicit `zones`: the default `(DK1, DK2)` raises `ValueError`, naming the zones to pass.
  - `EntsoeClient.capacity_market(product, start, end, *, zones=(NO1, ..., NO5))` returns FCR-N
    capacity for the Norwegian zones from 2014-09-30T22:00Z (ENTSO-E A81). ENTSO-E does not name the product: the series
    is labelled FCR-N because the identical query returns Energinet's FCR-N figures for SE and DK2.
  - The `Direction` docstring and `docs/API_CONTRACT.md` state the meaning of `NONE` on
    `ReserveCapacity`.

### Behaviour changes to note
- Three checks are stricter. Data that the 0.2.0 candidate accepted now raises `SourceError`: an eSett or
  Svenska kraftnät record that lacks a value key (a null value is still `None`), and any HTTP
  status other than 200 on a data request. Valid responses from the five providers parse as
  before; the live suite was rerun on this version.
- The ENTSO-E request-failure message now ends with the exception class, for example
  `entsoe: request failed (ConnectError)`. Code that compared the whole message must allow for it.
- Audit fixes make more malformed input raise. Valid responses from all five providers parse as
  before: the live suite was rerun on this version, and history scans (Energi Data Service FCR
  2021 to date, ENTSO-E 2024 to date) were run during the audit fixes.
  - **ENTSO-E:** a singleton field read by the parser that appears twice raises `SourceError`
    instead of using the first value; positions and amounts must be ASCII `xs:positiveInteger` /
    `xs:decimal` text (`1_0`, `1e5` and non-ASCII digits raise; `+2`, `.5` and `5.` stay valid);
    the document namespace must be a complete
    `urn:iec62325.351:tc57wg16:451-6:balancingdocument:<major>:<minor>` URN.
  - **Energi Data Service:** an imbalance record missing a requested column raises `SourceError`
    instead of reporting its values as `None`; a repeated `FcrNdDK2` auction row (same hour,
    area, product and auction type) raises, as a repeated `Total` already did.
  - **eSett:** `timestampUTC` accepts only the `Z` and `+00:00` designators, as the SvK and
    Energi Data Service parsers do; `-00:00`, `+00` and `+0000` raise `SourceError`. It also
    rejects date/time separators other than `T`, `t` or space.
  - **Fingrid:** timestamps require a `Z` or `+00:00` UTC designator; alternate zero-offset forms
    such as `-00:00`, `+00`, `+0000` and `+00:00:00` raise `SourceError`. An empty later page after
    the expected total is reached is reported as `page N returned no rows`.
  - **ENTSO-E timeInterval:** timestamps require extended date/time syntax with `T` and a `Z` or
    `+00:00` designator; alternate zero-offset forms, space separators and basic layouts raise
    `SourceError`.
  - **Energi Data Service FCR:** every numeric column is validated, including those kept only in
    `raw` and in `raw["auctions"]`.
  - **JSON responses:** a body whose nesting exhausts the decoder raises `SourceError` instead of a
    raw `RecursionError`.
  - **All clients:** a `base_url` that is not an absolute http or https URL raises `ValueError`
    at construction instead of a raw `httpx.InvalidURL` or `ValueError` on the first request.
  - **Retry-After:** delay-seconds accepts ASCII digits with an optional decimal fraction. Forms
    such as `5_0`, `+120`, `-3` and `1e400` use the default 10-second delay (`-3` previously meant
    no wait), and a delay above 300 s still raises `RateLimitError`.
  - **Market changes:** `changes_between(start, end)` raises `ValueError` when `end` is before
    `start`, as the client query methods do; it previously returned an empty list. `start == end`
    still returns an empty list.
  - The ENTSO-E HTTP error message no longer has an empty path slot: `entsoe: HTTP 404: <detail>`
    instead of `entsoe: HTTP 404 for : <detail>`.

### Fixed
- **Energi Data Service (High):** queries spanning more than one page were silently truncated to the first
  10,000 rows: the API reports `total` as the row count of each page, not of the whole query. Paging now
  continues until a page returns fewer than 10,000 records, and a page whose `total` differs from its record
  count raises `SourceError`. FcrNdDK2 requests also sort by a key unique per row, which offset paging needs.
  0.1.1 is affected: re-fetch any Energi Data Service imbalance-price query that returned exactly
  10,000 records (for both Danish zones, any window longer than about 52 days).
- **Svenska kraftnät:** `capacity_market` with a product SvK does not publish (FCR, FCR-N, FCR-D)
  raises `ValueError` before any request; it previously leaked a `KeyError`.
- **HTTP:** an undecodable response body (for example a corrupt `Content-Encoding`) is retried
  within `max_retries` like a dropped connection instead of failing after one request. Error
  messages no longer end in a bare `: ` when the error body is empty.
- **Records:** `raw` deep-copies any value that is not a mapping, list or tuple (for example a
  caller-supplied `set` or `bytearray`), so it is never shared with caller input or between records.
- **ENTSO-E capacity:** long FCR-N and mFRR queries no longer fail solely because
  the response exceeds ENTSO-E's 100-instance limit. Requests use seven-day chunks;
  instance-limit acknowledgements trigger recursive halving at the nearest hour,
  down to one hour. Whole-day clipping, HTTP retries and secret redaction are retained.
  Imbalance-price request sizing is unchanged.
- **ENTSO-E FCR-N:** identical repeated TimeSeries or documents now collapse to
  one set of capacity records, including the observed 2026-09-22 delivery day.
  Differing quantities or prices and repeated positions within a Period still raise.
  The zero-placeholder exception remains mFRR-only.
- **eSett:** a record missing any of `imblSalesPrice`, `imblPurchasePrice`, `upRegPrice`,
  `downRegPrice` or `mainDirRegPowerPerMBA` now raises `SourceError` naming the key. Before, a
  renamed or dropped column would have been read as `None` and reported as unpublished. eSett
  sends every key, with `null` for an unpublished value (checked live for 2021, 2022, 2023 and
  2026 records), so a null value still gives `None`.
- **Svenska kraftnät:** a record missing `price` or `volume` raises `SourceError`; a null value is
  still `None`.
- **ENTSO-E:** any error raised while sending a request (for example by a custom `httpx`
  transport) is reported as `SourceError("entsoe: request failed (<exception class>)")`, so the
  token in the request URL can't appear in the message or the traceback. The library's own
  errors, including `RateLimitError`, are unchanged.
- **Energi Data Service:** FCR-N and FCR-D requests filter on `ProductName`, so only the
  requested product's rows are fetched.
- **All clients:** only HTTP 200 is accepted as data. A 2xx such as 202 (not yet processed) or
  206 (partial content) raises `SourceError` instead of being parsed as a complete response.

### Documentation
- README names the CC BY 4.0 data sources (Energinet, Svenska kraftnät, Fingrid) and
  Energinet's suggested attribution.
- `docs/API_CONTRACT.md` now states how a deprecation is announced (`DeprecationWarning` pointing
  at the caller, naming the version that removes it) and that the API snapshot test defines the
  public surface. The known limitations moved there from `docs/RELEASE_EVIDENCE.md`; each is
  marked accepted and names the test or live check that covers it, or says that none does.

## 0.2.0 — not published (included in 0.3.0)

This version was prepared but never published to PyPI; everything below ships in 0.3.0.

First beta. The public surface is now written down and tested, the supported Python and
dependency range is tested in CI, and every source is checked against its live API daily. The
fixes below come from an independent audit of 0.1.1; no public signature or record field
changed.

### Added
- `docs/API_CONTRACT.md`: the public API (names in `nordic_balancing.__all__`, their parameters,
  fields, `source` values and exceptions), and the compatibility policy that applies from 1.0.
  `tests/test_public_api.py` fails on any change to that surface.
- `docs/FIELD_MATRIX.md`: which record fields each source provides, and how each source shows an
  interval it has not published. `None` in a field a source does not provide is now part of the
  contract; `tests/test_field_matrix.py` enforces the matrix.
- Python 3.14 support (classifier and CI), and a CI job at the lowest declared dependency
  versions (`httpx` 0.27.0, `pandas` 2.1).
- Daily scheduled live tests (`.github/workflows/live.yml`), including a recent window for each
  source and a check that Svenska kraftnät still publishes hourly capacity records.
- Hypothesis property tests for intervals as each source publishes them.
- `examples/benchmarks/`: memory and wall time for a one-year, 12-zone fetch (results in
  `docs/RELEASE_EVIDENCE.md`).
- `examples/entsoe_validation/validate.py multi-chunk`: ENTSO-E against eSett across the client's
  366-day request boundary.
- `SECURITY.md` (private vulnerability reporting) and `CONTRIBUTING.md`.

### Changed
- Development status is now `4 - Beta`.

### Behaviour changes to note
- Several checks are stricter: data that 0.1.1 accepted, or failed on with an unrelated exception,
  now raises `SourceError` (ENTSO-E documents and records, pagination totals, timestamps,
  non-success HTTP statuses). Each case is listed under Fixed. Valid responses from the five
  providers parse as before; the live suite and the ENTSO-E/eSett comparisons were rerun on this
  version.
- `FingridClient` and `EntsoeClient` raise `ValueError` at construction for an API key with
  internal whitespace or non-printable characters; surrounding whitespace is stripped.

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
- **SvK (pagination):** the total from the first page is kept; a later
  page reporting a different total raises. Before, a smaller later total turned an incomplete
  fetch into success.
- **Energi Data Service, SvK (timestamps):** a date without a time of day, or a non-UTC offset
  (including one that would move the value past the datetime limits), raises `SourceError`.
  Naive timestamps (what both providers send) are read as before; explicit UTC designators
  (`Z`, `+00:00`) are accepted by both (SvK rejected them before).
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
- **ENTSO-E (acknowledgements):** an acknowledgement means "no data" only when every text of
  every reason says so; any other text, including an empty one, raises. Before, one "No matching
  data found" reason hid the others, and a reason combining that text with another message was
  read as no data.
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
