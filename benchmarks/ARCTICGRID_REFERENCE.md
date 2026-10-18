# ArcticGrid as a reference consumer of nordic-balancing

Status: specification plus the smallest runnable integration, 2026-10-18. Written by the lead
(Opus) after reading ArcticGrid at `main` f7d4e64. Not independently reviewed.

## 1. The question this integration answers

ArcticGrid is a reliability harness for energy software: deterministic workloads, injected
faults, domain assertions, replay. It is **not** a market model, a battery optimiser or a
revenue calculator. A use case that fits it is a **data-integrity** question:

> Does a downstream consumer of Nordic market data stay correct when the inputs contain
> unpublished intervals, the 25-hour October day, and the hourly-to-15-minute resolution change,
> and how large is the error when a consumer gets "unpublished" wrong?

That is a real engineering question: every consumer of these feeds has to handle exactly these
cases, nordic-balancing's contract (`None` means unpublished or not provided, never 0;
`[start, end)` UTC intervals; `resolution` is the publication cadence) exists to make them
explicit, and silent mistakes here produce plausible-looking wrong numbers.

It is not a question about whether a battery should trade imbalance prices. The rule used below
is a probe, and its EUR figures are not a revenue estimate.

## 2. What ArcticGrid already has (verified in the code)

| Capability | Where | Used here |
|---|---|---|
| Immutable battery with SoC bounds, charge/discharge efficiency, power limits | `domain/battery.py`, `domain/units.py` | yes |
| Deterministic dispatch engine over offers | `flexibility/dispatch.py` | no (offer-based, not price-based) |
| Decimal settlement, banker's rounding, content-hashed ids | `settlement/settlement.py` | pattern reused (Decimal, quantize) |
| Deterministic scenarios, compare, offline replay of bundles | `scenarios/`, `compare/`, `replay/` | not yet (see section 5) |
| Daily collector + JSON store + read API for nordic-balancing data | `nordic/`, `api/routes/nordic.py` (merged 2026-10-18) | source of live datasets; not used by the demo, which needs frozen inputs |

## 3. What ArcticGrid does not have

- **No time-varying market price input** in simulation or settlement: settlement prices each
  delivery at its offer's fixed rate. Market prices exist only in the collector's store.
- No imbalance settlement semantics (single/dual pricing, direction-dependent prices), no bids,
  no activation, no forecasting, no degradation model.
- No scenario type that consumes an external dataset.

So the integration cannot be "plug prices into an existing simulation". The smallest honest
step is a separate replay script on top of ArcticGrid's battery model.

## 4. Architecture and data flow (no coupling between the projects)

```
frozen fixture ──► export_dataset.py ──► dataset.json ──► replay_battery.py ──► summary.json
(recorded HTTP)    nordic-balancing       immutable,        ArcticGrid battery     deterministic,
                   public API, replay     sha256 + lib      model only, stdlib      offline
                   transport              version + call    + hash check
```

- The two halves share only a JSON file with a hash. nordic-balancing is never imported by the
  ArcticGrid side and vice versa; ArcticGrid is installed from a pinned commit for the replay.
- The dataset records the library version, the exact client call and the fixture's recording
  time, so any result can be traced to its inputs.
- `replay_battery.py` refuses to run if the points do not match the recorded hash.

## 5. Smallest reproducible demonstration

```
# 1. export (nordic-balancing worktree, frozen fixtures, no network)
uv run --group benchmarks python benchmarks/arcticgrid_reference/export_dataset.py \
    --set entsoe-imb-SE3-year-2025 --out <dir>/se3-2025.json
# 2. replay twice (ArcticGrid pinned by commit, no nordic-balancing installed)
uv run --no-project --with "arcticgrid @ git+https://github.com/Razikale365/ArcticGrid@f7d4e64" \
    python benchmarks/arcticgrid_reference/replay_battery.py <dir>/se3-2025.json
```

Results (2026-10-18, Windows host, ArcticGrid f7d4e64, nordic-balancing `wp/perf` a16e844 for the
export; battery 1 MW / 2 MWh, efficiencies 0.95/0.95, SoC 10-90 %, start 50 %):

| dataset | intervals | resolution mix | unpublished | gaps | replay x2 |
|---|---:|---|---:|---:|---|
| ENTSO-E SE3 2025 (frozen fixture) | 35,020 | 7,276 hourly-published + 27,744 quarter-hour | 0 | 4 (20 quarters on 2025-01-06, -01-22, -02-08, -02-09) | byte-identical |
| eSett SE3 2025 (frozen fixture) | 35,040 | quarter-hour | 0 | 0 | byte-identical |
| eSett SE1, 2026-10-10..16 (ArcticGrid collector store) | 672 | quarter-hour | 1 (2026-10-16 02:15) | 0 | byte-identical |

What this shows:
- **Missing is not unpublished.** ENTSO-E's 2025 SE3 series lacks 20 quarters outright, while eSett
  has all 35,040: the library returns the gap as absent intervals and never invents values, and a
  consumer must detect gaps from `[start, end)` continuity (the replay counts them).
- **Unpublished as 0 is a silent error.** On the collector week, the one unpublished eSett quarter
  makes a null-as-zero consumer charge at a "price" of 0 (below its 3.04 EUR/MWh threshold). Its
  trajectory digest changes, yet its cash total is identical to the correct consumer's
  (1,033.87 EUR): the purchase was booked at 0 EUR when the real price was unknown (ENTSO-E
  published 0.07 EUR/MWh for that quarter). Nothing in the aggregate reveals the mistake.
- **Resolution change and DST** need no special handling downstream when the consumer uses
  `start`/`end` per record: UTC days are always 96 quarters, and hourly-published values arrive as
  four quarter records with `resolution_s = 3600`.
- **Determinism:** the same dataset hash gives a byte-identical summary, offline, with
  nordic-balancing not installed on the replay side.

Limits: one battery rule, three datasets, a non-dedicated Windows host (no timing claims are made
here), and no review of the rule by a domain expert. The EUR figures are not revenue estimates.

## 6. Next step if this proves useful (not done)

A scenario type in ArcticGrid that takes a dataset file (by hash) as its price input, plus domain
assertions such as "no interval settled at a price that was unpublished", "interval coverage
equals the dataset's", and "result digest equals the recorded digest". That belongs in ArcticGrid
and needs its own design review; nothing in nordic-balancing has to change for it.

## 7. Future multi-user stress test (specification only; not built)

Purpose: measure how a service built on nordic-balancing behaves when many users ask for
overlapping data, after single-process performance is understood (it now is, see
`PERFORMANCE_BASELINE.md`).

- **System under test:** a thin FastAPI service (ArcticGrid's API is the natural host) that answers
  `GET /prices?zones&start&end` from nordic-balancing clients, with an optional shared cache keyed
  by (source, zone, UTC day). The library itself stays synchronous (decision D8); the service runs
  calls in a bounded thread pool.
- **Load:** 1, 10, 50, 200 concurrent virtual users; request mix of one day / one month / one year,
  one zone / all zones; 30-50 % overlapping windows. Upstream is the frozen-fixture replay
  transport with injected latency drawn from the recorded `elapsed_s`, plus fault injection
  (429 with Retry-After, 5xx, timeouts) so no live provider is ever load-tested.
- **Measures:** p50/p95/p99 latency, throughput, error rate by type, peak and steady RSS, upstream
  request count (cache effectiveness), and correctness (responses byte-equal to single-user
  results).
- **Constraints already known:** a one-year all-zone ENTSO-E result holds about 413k records and
  ~640 MiB peak RSS in one process (Sol's comparison), so year-scale requests per user will not
  fit naively in memory at 50+ users. Expect the design to need day-granular caching and
  streaming responses before the multi-user numbers mean anything. Provider rate limits
  (Energi Data Service in particular) make request coalescing a correctness requirement, not
  only a performance one.
- **Not before:** the 1.0 contract is frozen; this is a post-1.0 investigation.
