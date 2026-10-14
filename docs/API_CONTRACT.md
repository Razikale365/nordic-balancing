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

Every client enforces the same data contracts (C1–C8 in [README.md](../README.md));
any violation raises `SourceError`.

### Intervals and resolution

- `start` (`datetime`): the inclusive start of the 15-minute settlement interval as
  a timezone-aware UTC `datetime` (`tzinfo` set with zero UTC offset). Passing a
  naive or non-UTC `start` to `ImbalancePrice` or `ReserveCapacity` raises
  `ValueError`.
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
- `zone: BiddingZone` — Nordic bidding zone (`SE1`–`SE4` for `SvKClient`).
- `product: ReserveProduct` — reserve product (`ReserveProduct.MFRR = "mFRR"` or
  `ReserveProduct.AFRR = "aFRR"`).
- `direction: Direction` — regulation direction (`Direction.UP` or
  `Direction.DOWN`).
- `price_eur_per_mw: float | None` — capacity price in EUR per MW per hour.
- `volume_mw: float | None` — procured capacity volume in MW.
- `source: str` — provider identifier (`"svk"`).
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
  surface defined in this document.
- **Deprecations**: deprecated public names, parameters, methods, or fields emit a
  warning for at least one minor release before removal.
- **Provider-side changes**: a provider-side API or payload change that makes a
  client raise on valid upstream data is addressed in a bug-fix release.
- **Tightened validation**: tightening a check so that previously accepted data now
  raises is a documented behaviour change in a minor release.
- **0.x series**: during `0.x`, breaking changes may occur between releases and are
  listed in [CHANGELOG.md](../CHANGELOG.md).
