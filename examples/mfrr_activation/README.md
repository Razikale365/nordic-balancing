# Reading Fingrid's mFRR activation datasets

`activation_check.py` fetches four Fingrid datasets for one 48-hour window with
`FingridClient.series` and prints what the data shows about its own conventions. It needs no
library change; `series()` already returns these datasets (15-minute records only).

| Dataset | Fingrid name | Unit | Content |
|---|---|---|---|
| 375 | Activated mFRR balancing regulation upward sum | MW | scheduled (SA) and direct (DA) activation, maximum power of the quarter hour |
| 376 | Activated mFRR balancing regulation downward sum | MW | the same, downward |
| 369 | Dominating direction in the mFRR energy market in Finland | -1, 0, 1 | -1 down, 1 up, 0 none; described per hour from which direction had more activated energy, published per quarter |
| 319 | Imbalance price | EUR/MWh | single price, single position |

Datasets 375 and 376 have 15-minute records from 2025-03-04, the go-live of the mFRR energy
activation market (sources in [`../mfrr_transition/README.md`](../mfrr_transition/README.md)). Datasets 385/386 (SA only) and 389/390 (DA only) publish the scheduled and
direct parts separately; they are not used here.

Window: `[2026-06-17T00:00Z, 2026-06-19T00:00Z)`, 192 quarters, picked from 10-24 June 2026
for its heavy activation in both directions. It is not a random sample, so the shares below
describe this window only.

Run (needs `FINGRID_API_KEY` in the environment; four requests):

```powershell
uv run python examples/mfrr_activation/activation_check.py --live
```

## Output (live API values, retrieved 2026-11-03)

```text
window 2026-06-17T00:00Z to 2026-06-19T00:00Z: 192 quarters

Sign of the 'downward' dataset 376
  values > 0: 0, values < 0: 147
  down quarters found by the naive 'value > 0': 0; by 'value < 0': 147

Share of quarters by activation (375 > 0 is up, 376 < 0 is down)
  both       121 of 192 (63%)
  up_only     45 of 192 (23%)
  down_only   26 of 192 (14%)
  none         0 of 192 (0%)

Dominating direction 369 changes within the hour in 20 of 48 hours

Dominating direction 369 against single-direction quarters
  68 of 71 agree; disagreeing quarters:
    2026-06-17T16:15Z up=35.0 down=0.0 dominating=-1.0
    2026-06-17T16:30Z up=46.0 down=0.0 dominating=-1.0
    2026-06-18T23:00Z up=195.0 down=0.0 dominating=-1.0
  quarters with both peaks: dominating up 24, down 97, none 0

Imbalance price 319 by class (EUR/MWh; descriptive, one window, not causal)
  up_only    n= 45 median=  64.42 IQR=[51.12, 74.58]
  down_only  n= 26 median=  -3.85 IQR=[-7.34, -0.79]
  both       n=121 median=  11.13 IQR=[2.22, 26.96]

375 and 376 are the maximum power of each quarter hour in MW. They are not
converted to MWh and the two directions are not netted.
```

## What it shows

- **Down is negative.** Dataset 376 is published as zero or negative MW (147 of 192 quarters
  below zero, none above). The metadata does not say so; reading "down active" as `value > 0`
  finds no activation at all.
- **The values are peak power.** Fingrid describes them as the maximum power of the quarter
  hour. They are neither average power nor energy, so they are not converted to MWh here, and
  up and down are not netted: 63 % of the quarters have both an up and a down peak.
- **No quiet quarters.** Every quarter in this window has activation in at least one direction,
  so a comparison of "activation" against "no activation" has nothing to compare.
- **Dominating direction is not the sign of the peaks.** Fingrid defines 369 from activated
  energy and describes it "for each hour", but its values change within the hour in 20 of 48
  hours, so it is read per quarter here. In three quarters with only an upward peak it is -1,
  although 376 shows no downward activation in them; the cause is not established here.
- The price medians are descriptive for this window only and say nothing about cause.

## Limits

One 48-hour window, which contains no clock change, so daylight-saving handling is untested.
Values were retrieved on 2026-11-03 and may differ from what was first published. Other
windows may differ from this one.
