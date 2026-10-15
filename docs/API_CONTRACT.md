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
  not an API change.

`tests/test_public_api.py` compares the public surface against
`tests/api_snapshot.json` and fails on any unrecorded addition, removal, or
signature change. Regenerate the snapshot intentionally with:

```bash
UPDATE_API_SNAPSHOT=1 uv run pytest tests/test_public_api.py
```

## Record field semantics and data contracts

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
- `SvKClient.capacity_market(product, start, end, zones=(SE1, SE2, SE3, SE4))`
  returns `list[ReserveCapacity]` for Swedish aFRR and mFRR capacity markets.

Every client enforces the same data contracts (C1–C8 in [README.md](../README.md));
any violation raises `SourceError`.
See [FIELD_MATRIX.md](FIELD_MATRIX.md) for per-source field availability and how unpublished intervals appear.

### Intervals and resolution

- `start` (`datetime`): the inclusive start of the 15-minute settlement interval as
  a timezone-aware UTC `datetime` (`tzinfo` is `datetime.UTC`). Passing a naive
  `start`, or one with a non-zero UTC offset, to `ImbalancePrice` or
  `ReserveCapacity` raises `ValueError`; a `start` in another time zone at offset
  zero (for example `Europe/London` in winter) is converted to UTC.
- `end` (`datetime` property): the exclusive end of the interval, equal to
  `start + INTERVAL` where `INTERVAL = timedelta(minutes=15)`.
- Query window (`[start, end)`): client query methods require timezone-aware
  `start` and `end` datetimes (converting any UTC offset to UTC) with `end > start`
  and return only intervals whose `start` is in `[start, end)`, at any sub-second
  precision of `start` and `end`. Records returned by a source outside the
  requested query window raise `SourceError`.
- `resolution` (`timedelta`): the publication interval at which the source
  published the record (`timedelta(minutes=15)` or `timedelta(hours=1)`). When a
  source published an hourly value, it is expanded into four 15-minute records
  across that hour while `resolution` retains `timedelta(hours=1)`.
  `FingridClient.series()` expands hourly records only for datasets 319, 244, 106,
  and 369; an hourly record in any other dataset raises `SourceError`.

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
  all 12 zones for EDS aFRR, DK1/DK2 for EDS mFRR).
- `product: ReserveProduct` — reserve product (`ReserveProduct.MFRR = "mFRR"` or
  `ReserveProduct.AFRR = "aFRR"`).
- `direction: Direction` — regulation direction (`Direction.UP` or
  `Direction.DOWN`).
- `price_eur_per_mw: float | None` — capacity price in EUR per MW per hour.
- `volume_mw: float | None` — procured capacity volume in MW.
- `source: str` — provider identifier (`"svk"` or `"energidataservice"`).
- `resolution: timedelta` — publication interval at the source.
- `raw: Mapping[str, Any]` — read-only view of the underlying source record
  (`compare=False, repr=False`).

### Missing vs unpublished values, numbers, duplicates, and `raw`

- **Missing vs unpublished**: an interval that the source did not return is absent
  from the returned list; clients never fill gaps. A field set to `None` on a
  returned record means the source has not published that value yet or does not
  provide that field.
- **Numbers**: numeric source values must be finite JSON numbers convertible to
  `float` (`bool`, `NaN`, and infinities are rejected with `SourceError`).
- **Pagination and duplicates**: paginated fetches must match the total row count
  reported by the source. Duplicate source records for the same series key raise
  `SourceError`, with two documented exceptions:
  - ENTSO-E: an interval published under distinct price categories (`A04` and
    `A05`) at the same resolution with an equal price collapses to one record,
    keeping all categories in `raw["categories"]`.
  - Fingrid: where hourly and 15-minute records overlap around the March 2025
    transition (datasets 319, 244, 106, 369), the hourly value is kept before
    `2025-03-18T23:00Z` and the 15-minute value from then on, with the other
    preserved in `raw["<dataset>_superseded"]`.
- **`raw` isolation**: `raw` is a `MappingProxyType` over a deep copy made for that
  record alone; nested containers are never shared with caller input or across the
  four quarters of an expanded hourly record. Only the top-level mapping rejects
  writes, so treat nested values as read-only as well. `to_frame()` omits `raw`.
- **Transport**: HTTP, network, and protocol errors raise `SourceError`, never raw
  `httpx` exceptions. HTTP 429 and 503 responses are retried within `max_retries`
  and then raise `RateLimitError` (or immediately when `Retry-After` exceeds
  300 seconds). HTTP 502/504, timeouts, and dropped connections are retried within
  the same `max_retries` budget and then raise `SourceError`.

## Supported Python and dependencies

- **Python**: every CPython release `>= 3.12` that is still under upstream support
  (Python 3.14 is added in `0.2.0`).
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
  `tests/test_entsoe.py::test_live_matches_esett_prices_but_not_its_resolution_label`,
  and
  `tests/test_fingrid.py::test_hourly_records_expand_in_imbalance_prices_with_hourly_resolution`.
- **Accepted (part of the contract).** `None` meaning is defined per source, as
  stated in [FIELD_MATRIX.md](FIELD_MATRIX.md): a field that a source never
  provides is `None` on every record from that source, and in a field that a
  source does provide, `None` means the source returned no value (unpublished or
  null). Covered by
  `tests/test_field_matrix.py::test_field_matrix_doc_matches_dataclass_fields_and_expectations`,
  `tests/test_field_matrix.py::test_imbalance_price_never_provided_fields_are_none_and_column_mapping`,
  `tests/test_field_matrix.py::test_reserve_capacity_never_provided_fields_are_none_and_column_mapping`,
  `tests/test_energidataservice.py::test_parses_published_and_unpublished_intervals`,
  and `tests/test_esett.py::test_parses_published_and_null_prices_and_sorts`.
- **Accepted (part of the contract).** `EntsoeClient`'s strict parser raises
  `SourceError` on differing dual-category prices and on curve types `A02`, `A04`,
  and `A05`, which `entsoe-py` may tolerate. None of these occurred in the 60 live
  zone-windows checked. Conflicting dual-category prices are covered by
  `tests/test_entsoe.py::test_rejects_dual_prices_for_the_same_interval` and
  `tests/test_entsoe.py::test_fixture_v45_conflicting_dual_categories_raise`.
  Rejection of curve types `A02` and `A05` is covered by
  `tests/test_entsoe.py::test_rejects_malformed_documents`. Rejection of curve
  type `A04` is enforced by the parser but has no dedicated test or live check.
- **Accepted (part of the contract).** A possible FI imbalance-price formula
  change around `2026-06-01` is **UNVERIFIED**: no primary source was found, so it
  has no entry in `CHANGES` and no test coverage. The library applies no formula of
  its own; it reports what Fingrid and eSett publish.
- **Accepted (part of the contract).** Synchronous only: there is no async client;
  all clients are synchronous over `httpx.Client`. Public client signatures are
  pinned by `tests/test_public_api.py::test_public_api_snapshot`.
- **Accepted (part of the contract).** Python 3.12+ only (`requires-python = ">=3.12"`
  in `pyproject.toml`).
- **Accepted (part of the contract).** eSett history starts at
  `2021-10-31T23:00Z`; nothing earlier exists in the eSett API.
- **Accepted (part of the contract).** Reconciliation classifies differences only;
  `reconcile_imbalance_prices` never merges series or picks a source. Covered by
  `tests/test_reconcile.py::test_identical_records_produce_no_divergences`,
  `tests/test_reconcile.py::test_one_interval_can_have_several_divergences`,
  `tests/test_reconcile.py::test_zones_in_only_one_input_are_reported_not_silently_dropped`,
  and `tests/test_properties.py::test_reconcile_swapping_arguments_swaps_missing_counts`.
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
  `tests/test_fingrid.py::test_other_datasets_reject_hourly_records`,
  `tests/test_fingrid.py::test_other_datasets_reject_overlapping_granularities`,
  `tests/test_fingrid.py::test_imbalance_datasets_keep_their_hourly_policy`, and
  `tests/test_fingrid.py::test_live_hourly_imbalance_prices_expand_to_quarters`.
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
  `tests/test_energidataservice.py::test_accepts_exact_dominating_directions`,
  `tests/test_energidataservice.py::test_rejects_fractional_dominating_direction`,
  and `tests/test_energidataservice.py::test_rejects_malformed_records`.
  Acceptance of `DominatingDirection=null` is implemented but has no dedicated
  test or live check.
- **Accepted (part of the contract).** ENTSO-E checks that never triggered on live
  data (60 zone-windows and all 12 zones for 2025, checked 2026-10-12) stay
  strict so they fail loudly if the API changes:
  - A point outside the requested window raises `SourceError` (`entsoe-py`
    truncates its A85 results to the window; no extra points were observed).
    Covered by `tests/test_entsoe.py::test_rejects_malformed_documents` and
    `tests/test_entsoe.py::test_a03_fill_still_rejects_explicit_point_outside_chunk_window`.
  - A `TimeSeries` without `curveType` raises `SourceError`, although the element
    is optional in the XSD (every observed `TimeSeries` had `A03`). Covered by
    `tests/test_entsoe.py::test_rejects_malformed_documents`.
  - When present, the response area (`area_Domain.mRID` or
    `controlArea_Domain.mRID`) must equal the bidding-zone EIC sent as
    `controlArea_Domain` (it did in every observed document). Covered by
    `tests/test_entsoe.py::test_rejects_a_document_for_another_area` and
    `tests/test_entsoe.py::test_accepts_a_document_for_the_requested_area`.
- **Accepted (part of the contract).** ENTSO-E: the token is passed as the
  `securityToken` query parameter, as the API requires. If `INFO` logging is
  enabled for the `httpx` logger, `httpx` logs request URLs and therefore the
  token (documented risk; library exceptions redact the token, covered by
  `tests/test_entsoe.py::test_request_params_eic_codes_and_custom_base_url`,
  `tests/test_entsoe.py::test_http_errors_include_status_and_redact_token`, and
  `tests/test_entsoe.py::test_transport_error_does_not_leak_token_or_url`).
- **Accepted (part of the contract).** SvK: `SvKClient` queries on-the-hour
  `start_time_utc` timestamps, rejects any record whose `start_time_utc` is not on
  the hour (`tests/test_svk.py::test_rejects_malformed_records`), and expands each
  hourly record into four 15-minute records
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
