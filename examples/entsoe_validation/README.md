# ENTSO-E vs eSett validation

`validate.py` compares `EntsoeClient` (ENTSO-E A85) with `ESettClient` interval by
interval, over three fixed comparisons:

| Mode | UTC windows | Requests |
|---|---|---|
| `samples` | 2022-01-15 (24 h); 2023-05-21T12:00Z, 2025-03-18T12:00Z and 2025-10-25T22:00Z (24 h each); 2026-10-10 (48 h). 60 zone-windows, 72 zone-days | 12 ENTSO-E + 1 eSett query per window |
| `year-2025` | 2025-01-01T00:00Z to 2026-01-01T00:00Z | 12 ENTSO-E (parallel, about 100 s) + 1 paged eSett query |
| `multi-chunk` | DK1 and SE3 only, 2024-06-01T00:00Z to 2025-07-01T00:00Z (395 days) | 2 ENTSO-E per zone, split at the 366-day chunk boundary 2025-06-02T00:00Z, + 1 paged eSett query; then 2025-05-31 to 2025-06-04 again in one request per zone |

The windows are fixed in the script; there is no option to widen them.

## Running

The script calls live APIs only with `--live`; without it, it exits before any
request. ENTSO-E needs `ENTSOE_API_KEY` in the environment. eSett needs no key.

```
uv run python examples/entsoe_validation/validate.py samples --live
uv run python examples/entsoe_validation/validate.py year-2025 --live
uv run python examples/entsoe_validation/validate.py multi-chunk --live
```

Each run writes `results_<mode>.json` next to the script (or to `--out`); these files are
not committed, and the numbers below summarise them.

## Method

Both sources are queried for `[start, end)`, and every (zone, interval start) in the
union of the two results is put in exactly one category:

| Category | Meaning |
|---|---|
| `missing_in_entsoe` / `missing_in_esett` | only the other source has the interval |
| `both_unpublished` | both have it, both prices `None` |
| `entsoe_unpublished` / `esett_unpublished` | one side's price is `None` |
| `exact` | equal prices |
| `rounding` | differ by at most 0.01 EUR/MWh (cent-precision float noise counts as 0.01) |
| `zero_price` | one side is exactly 0.0 and the other differs by more than 0.01 |
| `material` | any other difference above 0.01 |

The category counts add up to the size of the union, so nothing is counted twice.
`resolution_label_differs` counts intervals present in both with different
`resolution` labels. It is reported beside the categories, not as one, because the
two sources label equal prices differently (README, contract C7).

The JSON holds, per window, the totals, counts per zone, the missing intervals as
contiguous runs, and every interval that is not `exact` or missing, with both prices.
`tests/test_entsoe_validation.py` tests the classification offline.

## Results (`samples` and `year-2025`: run 2026-10-12)

`samples` (`results_samples.json`): 6,912 intervals, all present in both sources;
6,896 exact and 16 rounding (DK1 5, DK2 11). Resolution labels differ in 656
intervals, all in the 2023-05-21 window.

`year-2025` (`results_year-2025.json`): 420,480 intervals.

| Category | Count | Where |
|---|---:|---|
| `missing_in_entsoe` | 7,361 | DK1 3,609, DK2 3,610; NO1–NO4 12 each, NO5 14, SE1–SE4 20 each; FI 0 |
| `exact` | 411,972 | |
| `rounding` | 1,057 | DK1 508, DK2 549 |
| `zero_price` | 86 | DK1 43, DK2 43 |
| `material` | 4 | DK1 2025-06-29T17:45Z, FI 2025-04-29T02:30Z, NO3 and NO4 2025-04-24T18:45Z |
| all others | 0 | |

The DK1 2025-06-29T17:45Z value (ENTSO-E 2141.93, eSett 59.70) is also what Energi
Data Service publishes as the imbalance price; it equals that quarter's aFRR up price,
while eSett's equals its mFRR up price (`docs/SOURCE_NOTES.md`).

These match the counts from the first probe that `docs/SOURCE_NOTES.md` was
written from. The 0.1.0 text named only the three material differences outside DK.
ENTSO-E's published data can still be revised, so a later run may differ.

## Results (`multi-chunk`: run 2026-10-15)

`results_multi-chunk.json`: 75,840 intervals (2 zones × 395 days × 96). This is the only
committed run whose requests cross the client's 366-day chunk boundary.

| Category | DK1 | SE3 |
|---|---:|---:|
| `missing_in_entsoe` | 3,570 | 48 |
| `exact` | 34,117 | 37,872 |
| `rounding` | 189 | 0 |
| `zero_price` | 43 | 0 |
| `material` | 1 | 0 |
| all others | 0 | 0 |

Around the boundary (2025-05-31 to 2025-06-04) the two-chunk result equals the
single-request result: 768 intervals, identical prices and resolutions. The 2025
part repeats the `year-2025` findings for these zones (the DK1 January gap, the 43
zero quarters, the DK1 2025-06-29T17:45Z value). The 2024 part adds whole hours
missing from ENTSO-E: SE3 on 2024-09-11, 2024-10-11, 2024-10-13, 2024-11-09,
2024-11-18, 2024-12-10 and 2024-12-16, DK1 on 2024-10-11 and 2024-11-05 (every run
is in the JSON). Nothing is missing in eSett, and no price is `None` on either side.
