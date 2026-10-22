# Public API Contract

This document defines the public interface of `nordic-balancing`, the semantics of
its normalised records, its Python and dependency support policy, and its
compatibility rules.

## Public API surface

The public API consists of:

- Every name listed in `nordic_balancing.__all__`:
  - Client classes: `EnergiDataServiceClient`, `ESettClient`, `EntsoeClient`,
    `FingridClient`, `SvKClient`.
  - Record and report dataclasses: `ImbalancePrice`, `ReserveCapacity`,
    `MarketChange`, `Divergence`, `ReconciliationReport`.
  - Enums: `BiddingZone`, `Direction`, `ReserveProduct`, `DivergenceKind`.
  - Functions: `changes_between`, `reconcile_imbalance_prices`, `to_frame`.
  - Exceptions: `NordicBalancingError`, `SourceError`, `RateLimitError`.
  - Constants: `CHANGES`, `INTERVAL`, `__version__`.
- The documented constructor parameters, public methods, properties, dataclass
  fields (including field order and type annotations) and enum member names and
  values of those exported symbols.
- The `source` identifier strings on records and `RateLimitError`:
  `"energidataservice"`, `"esett"`, `"fingrid"`, `"svk"`, and `"entsoe"`.

Everything else is private:

- Submodules (`nordic_balancing.models`, `nordic_balancing.sources.*`,
  `nordic_balancing.reconcile`, `nordic_balancing.changes`,
  `nordic_balancing.frame`, `nordic_balancing.errors`, and internal `_` modules)
  are not public import paths; import public symbols from `nordic_balancing`.
- Any `_`-prefixed attribute, function, class, or module is private.
- For data constants (`CHANGES`, `INTERVAL`), only the type is part of the API
  surface. Adding a new dated `MarketChange` entry to `CHANGES` is a data update,
  not an API change. `CHANGES` covers changes to imbalance-price definitions;
  changes to a capacity market's definition (for example the EDS FCR-N/FCR-D
  price-basis change at `2024-02-01`) are documented per dataset under "Capacity
  market methods", not as `MarketChange` entries. `changes_between` raises
  `ValueError` when `end` is before `start`, and when `zones` is given but is
  empty or contains a value that is not a `BiddingZone` — a bare `BiddingZone`
  or zone-name string is not an iterable of zones and raises the same
  `ValueError` the client query methods raise for it.

`tests/test_public_api.py` compares the public surface against
`tests/api_snapshot.json` and fails on any unrecorded addition, removal, or
signature change. It also records whether each dataclass is frozen and slotted,
each enum's base class (`StrEnum` or `IntEnum`), each dataclass field's `compare`
and `repr` flags and `default_factory`, every public non-callable class attribute,
and public enum methods and properties.

Regenerate the snapshot intentionally with:

```bash
UPDATE_API_SNAPSHOT=1 uv run pytest tests/test_public_api.py
```

## Record field semantics and data contracts

Record and report dataclasses are slotted, so they have no `__dict__` and `vars(record)`
raises `TypeError`. Read fields by attribute access.

### Capacity market methods

- `EnergiDataServiceClient.capacity_market(product, start, end, *, zones=(DK1, DK2))`
  returns `list[ReserveCapacity]`: aFRR (`AfrrReservesNordic`) for all 12 Nordic
  zones from `2022-12-07T23:00Z`, or mFRR (`mFRRCapacityMarket`) for DK1/DK2 from
  `2023-06-20T22:00Z`. `product` must be a `ReserveProduct`, and `zones` must be a
  non-empty iterable of `BiddingZone` values. Other mFRR zones raise `ValueError`.
  Each hourly row supplies UP and DOWN marginal EUR/MW prices and procured MW,
  expanded to four quarters with hourly `resolution`. Procured volume includes
  cross-border allocation; demand and DKK prices remain in `raw`. Results sort
  by `(start, zone, direction)` and are filtered to `[start, end)`.
- EDS `ReserveProduct.FCR_N = "FCR-N"` and `ReserveProduct.FCR_D = "FCR-D"`
  use `FcrNdDK2` for DK2 and SE1–SE4 from `2021-11-09T23:00Z`. Only
  `AuctionType == "Total"` is emitted: `PurchasedVolumeLocal` is the area's MW
  and `PriceTotalEUR` is EUR/MW per hour. Other auction rows for the same hour,
  area and source product are retained in `raw["auctions"]`; auction rows
  without a Total, a repeated Total or a repeated auction of the same type for one
  hour, area and product, an unknown product name, or a row for another known
  product raise `SourceError`.
  FCR-N is symmetric (`Direction.NONE`); `FCR-D upp` maps to `UP` and
  `FCR-D ned` to `DOWN`. Before `2024-02-01` prices are average prices; from
  that date each auction has a marginal price and Total is the volume-weighted
  average of the auctions (common to all areas).
  On the last Sunday of October `FcrNdDK2` has no 00:00 UTC hour and publishes
  HourUTC 01:00 (HourDK 02:00) and 01:01 (HourDK 02:01) instead; these are
  returned as 00:00 and 01:00 UTC (verified against ENTSO-E A81 and SvK Mimer),
  with `raw` keeping the published `HourUTC`. Only that exact pattern is mapped:
  any other HourUTC off a 15-minute boundary, or a 01:01 row on a day that also
  has a 00:00 row, raises `SourceError`.
- EDS `ReserveProduct.FCR = "FCR"` uses `FcrDK1` for DK1 only, from
  `2021-01-18T23:00Z`:
  `FCRdomestic_MW` and `FCRdk_EUR` supply volume and price, with
  `Direction.NONE`; other source columns stay in `raw`. These FCR datasets
  expand hourly records to quarters with hourly `resolution`; null volume or
  price becomes `None`. Pass `zones=(BiddingZone.DK1,)` for FCR or
  `zones=(BiddingZone.DK2,)` / covered SE zones for FCR-N/FCR-D; the existing
  default `(DK1, DK2)` raises `ValueError` for all three products.
  On the last Sunday of October `FcrDK1` publishes no 01:00 UTC hour (the
  second 02:00 local); its other rows are labelled consistently, so that hour is
  absent from the result rather than mapped.
- `EntsoeClient.capacity_market(product, start, end, *, zones=(NO1, NO2, NO3, NO4, NO5))`
  returns FCR-N for Norwegian zones from `2014-09-30T22:00Z` (all five zones;
  checked live 2026-10-16), using A81/B95/A52 and market agreement A01. For
  FCR-N, documents must be A81/A52; series must be B95 with symmetric flow A03,
  quantity unit MAW and currency EUR. The mFRR bullet below states its own rules.
  `quantity` supplies MW and
  `procurement_Price.amount` supplies EUR/MW per hour; hourly points expand to
  quarters with `Direction.NONE`, retaining hourly `resolution`. A03 omitted
  positions continue the preceding quantity and price. Identical FCR-N quantity
  and price repeats collapse after A03 filling, whether from another document,
  ZIP member or TimeSeries. Differing values raise `SourceError`, retaining the
  existing FCR-N duplicate message. Repeated positions within one Period still
  raise. Other products/zones raise `ValueError`.
  Returned A81 delivery-day Periods are validated and A03-filled in full, then
  clipped to each query chunk; non-overlapping Periods contribute no records.
  Results sort by `(start, zone, direction)` and are filtered to `[start, end)`.
  ENTSO-E does not name the product; the series is FCR-N because the identical
  query returns EDS's FCR-N figures for SE and DK2. ENTSO-E's FCR-N documents for
  delivery days 2014-12-15 to 2014-12-19 and 2016-10-26 to 2016-11-03 are malformed
  (price and quantity in separate series, Periods spanning several days, and a price
  series scaled by 100), so a query covering those days raises `SourceError` rather
  than guessing. Every other delivery day from 2014-09-30T22:00Z to 2026-10-15 parses
  (scanned live 2026-10-16).
  Both FCR-N and mFRR capacity requests use **7-day chunks**. If an acknowledgement
  Reason text contains `exceeds the allowed maximum`, that capacity chunk is split
  at the hour nearest its midpoint (for an odd number of hours, the later hour) and each half is
  retried recursively, stopping at a minimum of one hour. An instance-limit
  acknowledgement at that floor raises `SourceError`. Each half reuses full
  delivery-day validation/A03 filling followed by clipping, preventing gaps and
  duplicate quarters. Existing HTTP retry and token-redaction rules are unchanged.
  A85 imbalance requests retain their 366-day chunk policy and do not use this
  capacity-only halving.
- `FingridClient.capacity_market(product, start, end)` returns FI `ReserveCapacity`
  for FCR-N (NONE, price/volume datasets 317/316, from 2014-01-01T00:00Z), FCR-D
  UP (318/315, from 2014-01-01T00:00Z), FCR-D DOWN (283/281, from
  2021-04-21T22:00Z), and mFRR UP/DOWN (329/327 and 330/328, from
  2022-11-30T23:00Z). Volume is hourly-market procured MW, excluding yearly-market
  FCR. Price and volume join on hour start, retaining an hour present in either
  dataset with the missing value `None`. Raw rows remain under `price` and `volume`.
  Each hourly level expands to four quarters with hourly `resolution`, clipped
  to `[start, end)` and sorted by `(start, zone, direction)`. Duplicate starts,
  off-hour starts, non-hourly durations and non-finite values raise `SourceError`.
  Other products raise `ValueError`; there is no `zones` parameter.
  `series()`, its hourly-policy dataset set and `imbalance_prices()` are unchanged.
- `EntsoeClient.capacity_market` also accepts `ReserveProduct.MFRR` for NO1–NO5
  and FI, keeping the NO1–NO5 default. It queries A81/B95/A47 with market agreement
  A01; requires EUR, MAW and PT60M; and maps flow A01 to UP and A02 to DOWN.
  A03 continuation, whole-day validation/clipping and expansion apply. As for
  FCR-N, identical quantity/price repeats from another document, ZIP member or
  TimeSeries in the same document collapse by `(zone, start, direction)`, comparing
  values after A03 filling. Conflicting repeats raise `SourceError`. Repeated
  positions within one Period of one series still raise.
  Before this comparison, mFRR-only placeholder Periods are ignored under a narrow
  rule: exactly one Point, at position 1, with quantity 0 and price 0, and another
  non-placeholder TimeSeries in the **same response** must provide values for the
  same zone, direction and exact Period start/end. Without that counterpart, the
  zero/zero series is kept as real data. Nonzero one-Point series, multiple-Point
  series and malformed series are not exempted. All source metadata and points
  are validated before filtering, independent of document/member order.
  Observed on delivery day **2025-06-05** (Period starts **2025-06-04T22:00Z**)
  for NO1-NO5 and FI: a live FI cross-check found the real series matched
  Fingrid **24/24 hours**, while the placeholder series matched **0/24**.
  After filtering, identical repeats collapse and differing values raise as above.
  This placeholder rule remains mFRR-only; it does not apply to FCR-N. FCR-N with FI raises
  `ValueError`: ENTSO-E has no Finnish FCR. The mFRR product was verified live
  against Fingrid (FI) and SvK
  (SE3), matching all 48 hour-directions per source on 2026-10-08. Reproduce with `examples/fi_no_capacity/cross_check.py`.
  ENTSO-E mFRR coverage starts at delivery day **2024-02-12** for NO1-NO5
  (first interval **2024-02-11T23:00Z**) and delivery day **2024-11-13** for FI
  (first interval **2024-11-12T23:00Z**), from a live historical search.
  Early documents repeat identical TimeSeries; the same mFRR collapse rule applies.
- `SvKClient.capacity_market(product, start, end, zones=(SE1, SE2, SE3, SE4))`
  returns `list[ReserveCapacity]` for Swedish aFRR and mFRR capacity markets.
  Other products raise `ValueError`.

Every client enforces the same data contracts (C1–C8 in [README.md](../README.md));
any violation raises `SourceError`.
See [FIELD_MATRIX.md](FIELD_MATRIX.md) for per-source field availability and how unpublished intervals appear.

### Intervals and resolution

- `start` (`datetime`): the inclusive start of the 15-minute settlement interval as
  a timezone-aware UTC `datetime` (`tzinfo` is `datetime.UTC`). Passing a
  `start` that is not a `datetime` raises `TypeError`. Passing a naive
  `start`, or one with a non-zero UTC offset, to `ImbalancePrice` or
  `ReserveCapacity` raises `ValueError`; a `start` in another time zone at offset
  zero (for example `Europe/London` in winter) is converted to UTC.
- `end` (`datetime` property): the exclusive end of the interval, equal to
  `start + INTERVAL` where `INTERVAL = timedelta(minutes=15)`.
- Query window (`[start, end)`): client query methods require timezone-aware
  `start` and `end` datetimes (converting any UTC offset to UTC) with `end > start`
  and return only intervals whose `start` is in `[start, end)`, at any sub-second
  precision of `start` and `end`. A `start`/`end` that is not a `datetime`
  raises `TypeError`; an aware datetime whose UTC conversion overflows the
  representable range (near `datetime.min`/`max`) raises `ValueError`, and so
  does any `start`/`end` outside the years 2 to 9997 in UTC.
  Records returned by a source outside the
  requested query window raise `SourceError`.
- `resolution` (`timedelta`): the publication interval at which the source
  published the record (`timedelta(minutes=15)` or `timedelta(hours=1)`). When a
  source published an hourly value, it is expanded into four 15-minute records
  across that hour while `resolution` retains `timedelta(hours=1)`.
  `FingridClient.series()` expands hourly records only for datasets 319, 244, 106,
  and 369; an hourly record in any other dataset raises `SourceError`. The EDS
  `ImbalancePrice` dataset publishes every quarter hour; a zone whose rows are
  all on the hour across more than one hour raises `SourceError` rather than
  returning records mislabelled as 15-minute.

### `ImbalancePrice`

Frozen, slotted dataclass representing the imbalance price and its components for
one bidding zone and one 15-minute UTC interval. Prices are in EUR/MWh unless the
field name states otherwise:

- `start: datetime` — UTC start of the 15-minute interval.
- `zone: BiddingZone` — Nordic bidding zone (`DK1`, `DK2`, `FI`, `NO1`–`NO5`,
  `SE1`–`SE4`).
- `imbalance_price_eur: float | None` — single imbalance settlement price in
  EUR/MWh.
- `imbalance_price_dkk: float | None` — imbalance settlement price in DKK/MWh.
- `spot_price_eur: float | None` — day-ahead spot price in EUR/MWh.
- `dominating_direction: Direction | None` — dominating regulation direction in
  the interval (`Direction.UP = 1` when the system was short and needed upward
  regulation, `Direction.DOWN = -1` when long, `Direction.NONE = 0`).
- `satisfied_demand_mw: float | None` — satisfied regulation demand in MW.
- `afrr_up_vwa_eur: float | None` — aFRR upward volume-weighted average price in
  EUR/MWh.
- `afrr_down_vwa_eur: float | None` — aFRR downward volume-weighted average price
  in EUR/MWh.
- `mfrr_up_price_eur: float | None` — mFRR upward regulating price in EUR/MWh.
- `mfrr_down_price_eur: float | None` — mFRR downward regulating price in EUR/MWh.
- `source: str` — provider identifier (`"energidataservice"`, `"esett"`,
  `"fingrid"`, or `"entsoe"`).
- `resolution: timedelta` — publication interval at the source.
- `raw: Mapping[str, Any]` — read-only view of the underlying source record
  (`compare=False, repr=False`).

### `ReserveCapacity`

Frozen, slotted dataclass representing procured reserve capacity and price for one
bidding zone, product, direction, and 15-minute UTC interval:

- `start: datetime` — UTC start of the 15-minute interval.
- `zone: BiddingZone` — Nordic bidding zone (`SE1`–`SE4` for `SvKClient`;
  all 12 zones for EDS aFRR, DK1/DK2 for EDS mFRR, DK2/SE1–SE4 for EDS
  FCR-N/FCR-D, DK1 for EDS FCR, FI for Fingrid, NO1–NO5 for ENTSO-E FCR-N,
  and NO1–NO5/FI for ENTSO-E mFRR).
- `product: ReserveProduct` — reserve product (`ReserveProduct.MFRR = "mFRR"`,
  `ReserveProduct.AFRR = "aFRR"`, `ReserveProduct.FCR_N = "FCR-N"`,
  `ReserveProduct.FCR_D = "FCR-D"`, or `ReserveProduct.FCR = "FCR"`).
- `direction: Direction` — `Direction.UP` or `Direction.DOWN` for products
  procured per direction (aFRR, mFRR, FCR-D). For a symmetric product (FCR-N,
  FCR) it is `Direction.NONE`: the volume is available in both directions and
  the price is paid once, so count such a record in both directions when summing
  volume per direction, and once when summing cost.
- `price_eur_per_mw: float | None` — capacity price in EUR per MW per hour.
- `volume_mw: float | None` — procured capacity volume in MW.
- `source: str` — provider identifier (`"svk"`, `"energidataservice"`, `"entsoe"`, or `"fingrid"`).
- `resolution: timedelta` — publication interval at the source.
- `raw: Mapping[str, Any]` — read-only view of the underlying source record
  (`compare=False, repr=False`).

### Missing vs unpublished values, numbers, duplicates, and `raw`

- **Missing vs unpublished**: an interval that the source did not return is absent
  from the returned list; clients never fill gaps. A field set to `None` on a
  returned record means the source has not published that value yet or does not
  provide that field. eSett, Svenska kraftnät and Energi Data Service records that
  lack a value key or a requested column raise `SourceError`; only an explicit null
  becomes `None`, except that eSett DK1/DK2 mark an unsettled interval with `0.0`
  in every price column (`imblSalesPrice`, `imblPurchasePrice`, `upRegPrice`,
  `downRegPrice`) and a `mainDirRegPowerPerMBA` of `0` or null, which maps to `None`
  the same way (prices and direction). An all-zero row with an up or down direction
  is a settled price and is kept.
- **Numbers**: numeric source values must be finite JSON numbers convertible to
  `float` (`bool`, `NaN`, and infinities are rejected with `SourceError`).
  ENTSO-E XML values must be ASCII `xs:decimal` text and positions ASCII
  `xs:positiveInteger` text (`1_0`, `1e5` and non-ASCII digits raise); a singleton
  field read by the parser that appears twice raises `SourceError` rather than using
  either value.
- **Pagination and duplicates**: paginated fetches must match the total row count
  reported by the source. Energi Data Service reports `total` per page: it pages by
  offset, with a sort key unique per row, until a page returns fewer than the page
  size, and a page whose `total` differs from its record count raises `SourceError`. Duplicate source records for the same series key raise
  `SourceError`, with the following documented exceptions:
  - ENTSO-E imbalance prices (A85): an interval published under distinct price
    categories (`A04` and `A05`) at the same resolution with an equal price
    collapses to one record,
    keeping all categories in `raw["categories"]`.
  - ENTSO-E FCR-N and mFRR capacity (A81): identical quantity/price repeats across
    documents, ZIP members or TimeSeries collapse by zone, quarter and direction
    after A03 filling; differing values and repeated positions within a Period
    raise. The separately documented placeholder filter applies only to mFRR.
  - Fingrid: where hourly and 15-minute records overlap around the March 2025
    transition (datasets 319, 244, 106, 369), the hourly value is kept before
    `2025-03-18T23:00Z` and the 15-minute value from then on, with the other
    preserved in `raw["<dataset>_superseded"]`.
- **`raw` isolation**: `raw` is a read-only `Mapping` over data private to that record
  (a deep copy, or a freshly decoded source row nothing else references); nested containers are never shared with caller input or across the
  four quarters of an expanded hourly record. Only the top-level mapping rejects
  writes, so treat nested values as read-only as well. Records — and the report
  dataclasses holding them — support `pickle`, `copy.deepcopy` and
  `dataclasses.asdict`; each produces detached copies. `to_frame()` omits `raw`
  and accepts records that are all exactly `ImbalancePrice` or all exactly
  `ReserveCapacity`; subclass instances raise `ValueError`.
- **Transport**: HTTP, network, and protocol errors raise `SourceError`, never raw
  `httpx` exceptions. HTTP 429 and 503 responses are retried within `max_retries`
  and then raise `RateLimitError` (or immediately when `Retry-After` exceeds
  300 seconds). `Retry-After` delay-seconds accepts ASCII digits with an optional
  decimal fraction; HTTP-date values are converted to a delay, and missing or
  malformed values use the default 10-second delay. HTTP 502/504,
  timeouts, dropped connections and undecodable response bodies are retried within
  the same `max_retries` budget and then raise `SourceError`. Every client raises
  `ValueError` at construction for a `base_url` that is not an absolute http or https
  URL. A query that needs HTTP after an owned client is closed raises `RuntimeError`
  for Energi Data Service, eSett, Fingrid and SvK; ENTSO-E wraps it as
  `SourceError("entsoe: request failed (RuntimeError)")`. Calling `close()` leaves a
  caller-supplied HTTP client open, although Fingrid closes its own authenticated
  wrapper.

## Supported Python and dependencies

- **Python**: every CPython release `>= 3.12` that is still under upstream support
  (Python 3.14 is added in `0.3.0`).
- **Runtime dependencies**: `httpx>=0.27,<1` is the only required runtime
  dependency, tested at the floor (`0.27.x`) and at the latest allowed release.
  When `httpx` 1.0 ships, it is evaluated in the next minor release.
- **Optional dependencies**: `pandas` (`nordic-balancing[pandas]`) stays an
  optional extra and is imported only when `to_frame()` is called.

## Compatibility and deprecation policy

*Applies from 1.0; during 0.x, breaking changes are listed in
[CHANGELOG.md](../CHANGELOG.md).*

- **Semantic Versioning**: from `1.0.0`, releases follow SemVer on the public API
  surface defined in this document. The API snapshot test
  (`tests/test_public_api.py::test_public_api_snapshot` against
  `tests/api_snapshot.json`) is the definition of the public surface.
- **Deprecations**: deprecated public names, parameters, methods, or fields emit
  `DeprecationWarning` with `stacklevel` pointing at the caller and the version
  that removes the feature named in the message, for at least one minor release
  before removal.
- **Provider-side changes**: a provider-side API or payload change that makes a
  client raise on valid upstream data is addressed in a bug-fix release.
- **Tightened validation**: tightening a check so that previously accepted data now
  raises is a documented behaviour change in a minor release.
- **0.x series**: during `0.x`, breaking changes may occur between releases and are
  listed in [CHANGELOG.md](../CHANGELOG.md).

## Known limitations

Every item below is classified for `1.0.0` as `accepted (part of the contract)` or
`to fix before 1.0`. No item is currently classified as `to fix before 1.0`.

- **Accepted (part of the contract).** `resolution` records publication cadence,
  not information content: quarters before `2025-03-18T23:00Z` are labelled
  15 minutes where the source published 15-minute intervals, even though the four
  quarters of an hour carried a single hourly value
  (`examples/mfrr_transition/README.md`;
  `examples/entsoe_py_comparison/REPORT.md`). Covered by
  `tests/test_esett.py::test_resolution_transition_preserves_source_publication_intervals`,
  `tests/test_esett.py::test_live_hourly_to_quarter_hour_transition`,
  `tests/test_entsoe_imbalance.py::test_live_matches_esett_prices_but_not_its_resolution_label`,
  and
  `tests/test_fingrid_imbalance.py::test_hourly_records_expand_in_imbalance_prices_with_hourly_resolution`.
- **Accepted (part of the contract).** `None` meaning is defined per source, as
  stated in [FIELD_MATRIX.md](FIELD_MATRIX.md): a field that a source never
  provides is `None` on every record from that source, and in a field that a
  source does provide, `None` means the source returned no value (unpublished or
  null). Covered by
  `tests/test_field_matrix.py::test_field_matrix_doc_matches_dataclass_fields_and_expectations`,
  `tests/test_field_matrix.py::test_imbalance_price_never_provided_fields_are_none_and_column_mapping`,
  `tests/test_field_matrix.py::test_reserve_capacity_never_provided_fields_are_none_and_column_mapping`,
  `tests/test_energidataservice_imbalance.py::test_parses_published_and_unpublished_intervals`,
  and `tests/test_esett.py::test_parses_published_and_null_prices_and_sorts`.
- **Accepted (part of the contract).** `EntsoeClient`'s strict parser raises
  `SourceError` on differing dual-category prices and on curve types `A02`, `A04`,
  and `A05`, which `entsoe-py` may tolerate. None of these occurred in the 60 live
  zone-windows checked. Conflicting dual-category prices are covered by
  `tests/test_entsoe_imbalance.py::test_rejects_dual_prices_for_the_same_interval` and
  `tests/test_entsoe_imbalance.py::test_fixture_v45_conflicting_dual_categories_raise`.
  Rejection of curve types `A02`, `A04` and `A05` is covered by
  `tests/test_entsoe_imbalance.py::test_rejects_malformed_documents`.
- **Accepted (part of the contract).** A possible FI imbalance-price formula
  change around `2026-06-01` is **UNVERIFIED**: no primary source was found, so it
  has no entry in `CHANGES` (`tests/test_changes.py::test_unverified_fi_formula_change_has_no_entry`).
  The library applies no formula of its own; it reports what Fingrid and eSett
  publish (`tests/test_field_matrix.py::test_imbalance_price_fields_map_to_their_source_columns`).
- **Accepted (part of the contract).** Synchronous only: there is no async client;
  all clients are synchronous over `httpx.Client`. Public client signatures are
  pinned by `tests/test_public_api.py::test_public_api_snapshot`.
- **Accepted (part of the contract).** Python 3.12+ only (`requires-python = ">=3.12"`
  in `pyproject.toml`). CI tests every classified version; covered by
  `tests/test_package.py::test_python_floor_is_3_12_and_ci_tests_every_classified_version`.
- **Accepted (part of the contract).** eSett history starts at
  `2021-10-31T23:00Z`; nothing earlier exists in the eSett API. Covered live by
  `tests/test_esett.py::test_live_history_starts_2021_10_31_23z`.
- **Accepted (part of the contract).** Reconciliation classifies differences only;
  `reconcile_imbalance_prices` never merges series or picks a source. Covered by
  `tests/test_reconcile.py::test_identical_records_produce_no_divergences`,
  `tests/test_reconcile.py::test_one_interval_can_have_several_divergences`,
  `tests/test_reconcile.py::test_zones_in_only_one_input_are_reported_not_silently_dropped`,
  and `tests/test_properties.py::test_reconcile_swapping_arguments_swaps_missing_counts`.
  `ReconciliationReport.divergences` is ordered by `(start, zone, kind)`, covered by
  `tests/test_reconcile.py::test_divergences_sorted_by_start_zone_kind` and
  `tests/test_reconcile.py::test_divergences_sort_zones_at_shared_start`.
  The exact ROUNDING/PRICE rule is: a difference `diff = abs(p - r)` is PRICE
  when `diff > tolerance_eur and not math.isclose(diff, tolerance_eur)` — so a
  difference that exceeds the tolerance only through binary float noise (for
  example `17.53 - 17.52 > 0.01` with the default `0.01`) is still ROUNDING —
  and ROUNDING when `diff` is non-zero and does not meet that test. Covered by
  `tests/test_reconcile.py::test_float_noise_at_tolerance_boundary_is_rounding`,
  `tests/test_reconcile.py::test_difference_exactly_at_tolerance_is_rounding`,
  and `tests/test_reconcile.py::test_rounding_divergence_within_tolerance`.
  A non-finite `imbalance_price_eur` (NaN or infinity) is neither "exactly
  equal" nor "within the tolerance": it is always a PRICE divergence, never
  identical or ROUNDING, covered by
  `tests/test_reconcile.py::test_non_finite_prices_are_price_divergences`.
- **Accepted (part of the contract).** `raw` is a read-only mapping over a deep
  copy private to each record. Only the top level rejects writes; nested
  containers are mutable but unshared, so treat them as read-only. Covered by
  `tests/test_models.py::test_raw_top_level_is_read_only`,
  `tests/test_models.py::test_raw_is_detached_from_the_source_record`,
  `tests/test_models.py::test_expanded_records_do_not_share_nested_raw`, and
  `tests/test_models.py::test_reserve_capacity_raw_is_detached_too`.
- **Accepted (part of the contract).** `FingridClient.series()` has a verified
  hourly policy only for datasets 319, 244, 106, and 369 (per-interval prices and
  direction). Hourly records from any other dataset raise `SourceError` rather
  than being repeated, since they may be additive totals. Covered by
  `tests/test_fingrid_imbalance.py::test_other_datasets_reject_hourly_records`,
  `tests/test_fingrid_imbalance.py::test_other_datasets_reject_overlapping_granularities`,
  `tests/test_fingrid_imbalance.py::test_imbalance_datasets_keep_their_hourly_policy`, and
  `tests/test_fingrid_imbalance.py::test_live_hourly_imbalance_prices_expand_to_quarters`.
- **Accepted (part of the contract).** eSett: a price published in only one of the
  `imblSalesPrice` or `imblPurchasePrice` columns is taken as the single price
  (never observed in ~107k records sampled 2021-11 to 2026-10; differing non-null
  values raise `SourceError`). Covered by
  `tests/test_esett.py::test_allows_one_or_both_single_prices_to_be_missing`,
  `tests/test_esett.py::test_purchase_only_price_is_kept_not_dropped`, and
  `tests/test_esett.py::test_rejects_sales_purchase_disagreement`.
- **Accepted (part of the contract).** Energi Data Service:
  `DominatingDirection` must be `-1`, `0`, `1`, or `null` (observed values in
  ~16.8k records: only those and `null`); any other value raises `SourceError`.
  Acceptance of the numeric values and rejection of invalid numeric values are
  covered by
  `tests/test_energidataservice_imbalance.py::test_accepts_exact_dominating_directions`,
  `tests/test_energidataservice_imbalance.py::test_rejects_fractional_dominating_direction`,
  and `tests/test_energidataservice_imbalance.py::test_rejects_malformed_records`.
  `DominatingDirection=null` becomes `None`
  (`tests/test_energidataservice_imbalance.py::test_null_dominating_direction_is_none`).
- **Accepted (part of the contract).** ENTSO-E checks that never triggered on live
  data (60 zone-windows and all 12 zones for 2025, checked 2026-10-12) stay
  strict so they fail loudly if the API changes:
  - In an A85 imbalance-price document, a point outside the requested window
    raises `SourceError` (`entsoe-py` truncates its A85 results to the window; no
    extra points were observed). A81 FCR capacity documents always cover whole
    delivery days, so `capacity_market` validates the whole Period and keeps only
    the points inside the window. Covered by
    `tests/test_entsoe_imbalance.py::test_rejects_malformed_documents`,
    `tests/test_entsoe_imbalance.py::test_a03_fill_still_rejects_explicit_point_outside_chunk_window`,
    `tests/test_entsoe_capacity.py::test_entsoe_capacity_clips_whole_delivery_day_after_a03_fill`
    and
    `tests/test_entsoe_capacity.py::test_entsoe_capacity_two_chunks_clip_same_delivery_day_without_duplicates`.
  - A `TimeSeries` without `curveType` raises `SourceError`, although the element
    is optional in the XSD (every observed `TimeSeries` had `A03`). Covered by
    `tests/test_entsoe_imbalance.py::test_rejects_malformed_documents`.
  - When present, the response area (`area_Domain.mRID` or
    `controlArea_Domain.mRID`) must equal the bidding-zone EIC sent as
    `controlArea_Domain` (it did in every observed document). Covered by
    `tests/test_entsoe_imbalance.py::test_rejects_a_document_for_another_area` and
    `tests/test_entsoe_imbalance.py::test_accepts_a_document_for_the_requested_area`.
- **Accepted (part of the contract).** ENTSO-E: the token is passed as the
  `securityToken` query parameter, as the API requires. If `INFO` logging is
  enabled for the `httpx` logger, `httpx` logs request URLs and therefore the
  token (documented risk, shown by
  `tests/test_entsoe_imbalance.py::test_token_reaches_logs_only_through_httpx_info_logging`,
  which also checks that the library leaves the `httpx` logger's level and
  handlers unchanged; library exceptions
  redact the token, covered by
  `tests/test_entsoe_imbalance.py::test_request_params_eic_codes_and_custom_base_url`,
  `tests/test_entsoe_imbalance.py::test_http_errors_include_status_and_redact_token`, and
  `tests/test_entsoe_imbalance.py::test_transport_error_does_not_leak_token_or_url`).
- **Accepted (part of the contract).** SvK: `SvKClient` filters on every
  quarter-hour `start_time_utc` of the requested hours, so a move to 15-minute
  rows is fetched rather than filtered out; any record whose `start_time_utc`
  is not on the hour raises `SourceError`
  (`tests/test_svk.py::test_rejects_malformed_records`), and each hourly record
  expands into four 15-minute records
  (`tests/test_svk.py::test_expands_hourly_records_and_sorts_by_start_zone_direction`).
  Every record observed so far is hourly, and the dataset has no duration column.
  In `tests/test_live_recent.py`, the daily live sentinel `test_live_recent_svk`
  fetches a recent 24-hour window (`today - 8 days` to `today - 7 days`) of mFRR
  capacity records via `SvKClient.capacity_market` and verifies that records are
  returned on the 15-minute UTC grid within `[start, end)`, while
  `test_svk_may_have_moved_to_15_minute_records` queries `datastore_search`
  directly for the 200 most recent `mfrr_capacity_market` rows
  (`sort="start_time_utc desc"`, `limit="200"`) without an hour filter and asserts
  that every `start_time_utc` has zero minute, second, and microsecond fields.
