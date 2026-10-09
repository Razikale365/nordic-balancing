# Cross-provider reconciliation

`reconcile_dk_fi.py` runs `nordic_balancing.reconcile_imbalance_prices` on two
provider pairs across six UTC days chosen around the dated market changes in
`nordic_balancing.CHANGES`:

- Energi Data Service vs eSett, DK1 and DK2
- Fingrid vs eSett, FI (skipped unless `FINGRID_API_KEY` is in the environment)

Reconciliation classifies differences; it never merges the series or decides
which source is right.

Run: `uv run python examples/reconciliation/reconcile_dk_fi.py`
Writes `results.json` (every divergence with both sides' values) next to this
file.

## Counts per day and pair

| UTC day | pair | compared | identical | divergences |
|---|---|---|---|---|
| 2025-03-10 | EDS vs eSett (DK1+DK2) | 192 | 130 | 62 direction |
| 2025-03-10 | Fingrid vs eSett (FI) | 96 | 0 | 96 resolution |
| 2025-03-19 | EDS vs eSett | 192 | 190 | 2 rounding |
| 2025-03-19 | Fingrid vs eSett | 96 | 96 | — |
| 2025-06-02 | EDS vs eSett | 192 | 188 | 4 rounding |
| 2025-06-02 | Fingrid vs eSett | 96 | 96 | — |
| 2025-10-02 | EDS vs eSett | 192 | 188 | 4 rounding |
| 2025-10-02 | Fingrid vs eSett | 96 | 96 | — |
| 2026-03-04 | EDS vs eSett | 192 | 184 | 8 rounding |
| 2026-03-04 | Fingrid vs eSett | 96 | 96 | — |
| 2026-09-15 | EDS vs eSett | 192 | 190 | 2 rounding |
| 2026-09-15 | Fingrid vs eSett | 96 | 96 | — |

No missing, unpublished or above-tolerance price divergence appeared on any
sampled day.

## RESOLUTION — FI on 2025-03-10 (verified)

All 96 FI intervals diverge on publication resolution alone: Fingrid hourly
(60min) vs eSett quarter-hourly (15min), e.g. FI 2025-03-10T00:00Z. Every price
is identical — eSett repeated the hourly value per quarter. This matches the
change at 2025-03-18T23:00Z, when imbalance pricing became genuinely
quarter-hourly. During the 2025-03-14→18 overlap Fingrid published both
granularities; `FingridClient` uses the hourly value before 2025-03-18T23:00Z
and keeps the losing record under `raw["<dataset>_superseded"]`. On 2025-03-10
no 15-minute records existed yet; on 2025-03-19 FI is fully quarter-hourly and
identical to eSett.

## DIRECTION — DK1+DK2 on 2025-03-10 (verified observation; hypothesis on cause)

62 of 192 intervals disagree on dominating direction with identical prices:
DK1 00:45Z `UP vs DOWN` at 61.25 EUR/MWh, DK1 18:00Z `UP vs NONE` at 38.27.
*Hypothesis:* under the Danish imbalance design replaced at 2025-03-18T23:00Z
the two fields were derived differently — eSett's `mainDirRegPowerPerMBA` vs
Energinet's `DominatingDirection`. On every sampled day after that change the
directions agree.

## ROUNDING — every sampled day (verified)

2–8 intervals per day differ by exactly 0.01 EUR/MWh, e.g. DK1
2025-03-19T07:00Z `281.81 vs 281.8` and DK1 2026-09-15T19:30Z `4.37 vs 4.38`.
*Hypothesis:* cent-level rounding between the two publication pipelines; the
tolerance (0.01 EUR) keeps these out of PRICE. The Energinet tolerance band on
2025-06-02 and the satisfied-demand direction rule on 2026-03-04 produced no
new divergence class; 2026-03-04 shows a slightly higher rounding count (8)
than a plain day.
