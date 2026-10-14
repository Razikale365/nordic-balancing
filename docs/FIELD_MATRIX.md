# Per-Source Field Availability Matrix

Per-source field availability is part of the public contract (see
[API_CONTRACT.md](API_CONTRACT.md)). A field a source does not provide is
`None` on every record from that source; in a field it does provide, `None`
means the source returned no value (unpublished or null). The library cannot
distinguish what the source does not distinguish.

Cell values in the tables below:

- `provided` — the client maps this field from the source response (nullable
  fields are `None` when the source returns `null` or omits the component).
- `—` — the source does not provide this field; it is `None` on every record
  from that source.
- `from <date>` — the source provides this field only from a live-verified
  date onwards (see [RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md) for dataset
  history start dates and resolution transitions).

## `ImbalancePrice`

| Field | Energi Data Service (`energidataservice`) | eSett (`esett`) | Fingrid (`fingrid`) | ENTSO-E (`entsoe`) |
|---|---|---|---|---|
| `start` | `provided` | `provided` | `provided` | `provided` |
| `zone` | `provided` | `provided` | `provided` | `provided` |
| `imbalance_price_eur` | `provided` | `provided` | `provided` | `provided` |
| `imbalance_price_dkk` | `provided` | `—` | `—` | `—` |
| `spot_price_eur` | `provided` | `—` | `—` | `—` |
| `dominating_direction` | `provided` | `provided` | `provided` | `—` |
| `satisfied_demand_mw` | `provided` | `—` | `—` | `—` |
| `afrr_up_vwa_eur` | `provided` | `—` | `—` | `—` |
| `afrr_down_vwa_eur` | `provided` | `—` | `—` | `—` |
| `mfrr_up_price_eur` | `provided` | `provided` | `provided` | `—` |
| `mfrr_down_price_eur` | `provided` | `provided` | `provided` | `—` |
| `source` | `provided` | `provided` | `provided` | `provided` |
| `resolution` | `provided` | `provided` | `provided` | `provided` |
| `raw` | `provided` | `provided` | `provided` | `provided` |
| Unpublished interval appears as | Record with `None` prices when the API returns the row with `null` price columns before settlement completes; no record if the row is absent | Record with `None` prices (trailing unpublished intervals arrive as rows with null prices; no record if the interval is absent) | No record when absent from dataset 319 (Fingrid returns one record per start present in its price dataset; a missing or null row in dataset 244, 106, or 369 leaves that component field `None`) | No record (ENTSO-E omits intervals it does not publish, and `A03` curves repeat the previous point only for omitted positions inside a `Period`; a `Point` without `imbalance_Price.amount` has `imbalance_price_eur=None`) |

For ENTSO-E A85 documents, optional `flowDirection.direction` and
`Financial_Price` elements are preserved in `raw` (`raw["flow_direction"]` and
`raw["financial_prices"]`) rather than mapped to normalised fields, because
their contribution to the single imbalance price is not verified.

## `ReserveCapacity`

| Field | Svenska kraftnät (`svk`) |
|---|---|
| `start` | `provided` |
| `zone` | `provided` |
| `product` | `provided` |
| `direction` | `provided` |
| `price_eur_per_mw` | `provided` |
| `volume_mw` | `provided` |
| `source` | `provided` |
| `resolution` | `provided` |
| `raw` | `provided` |
| Unpublished interval appears as | No record when the hourly row is absent from `datastore_search`; four quarter-hour records with `None` (`price_eur_per_mw` / `volume_mw`) when a row is returned with null values |

An hourly record expanded to quarters repeats a level (a price, or MW of
capacity), not an amount, so summing records over time is meaningless; a
quarter's capacity cost is `price_eur_per_mw × volume_mw × 0.25 h`.
