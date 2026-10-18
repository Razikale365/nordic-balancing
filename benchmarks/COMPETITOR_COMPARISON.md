# Frozen-payload competitor comparison — 2026-10-18

**Completed with explicit non-comparable workloads.** Eight real workloads were compared against
pinned public clients. All shared numeric prices match exactly. An independent XML audit confirms
complete source-supported coverage at each client's documented resolution. No live provider calls,
fixture changes, approximate matches or synthetic response bodies were used.

Nordic Balancing processed the tested A85 payloads faster, including retrieval plus `to_frame()`.
entsoe-py used less peak memory for the all-zone year. esett-py was about 9–10 times faster at
returning raw dictionaries and used less memory; that output has fewer market normalization and
validation guarantees. There is no universal performance winner.

## Survey and exact versions

PyPI JSON was retrieved directly on 2026-10-17. Search-engine release snippets were stale for
entsoe-py; the live registry reported 0.8.1. Machine-readable registry evidence is in
`results/compare-candidates.json`. Release activity alone does not establish ongoing maintenance.
Pinned comparison dependencies are isolated in `[dependency-groups].benchmarks-compare`;
none were added to the library's runtime dependencies. `uv.lock` pins their transitive dependencies.

| Client | Version | Last release (UTC date) | Licence evidence | Workload coverage / decision |
|---|---|---|---|---|
| [entsoe-py](https://pypi.org/project/entsoe-py/0.8.1/) | 0.8.1 | 2026-09-04 | MIT, PyPI metadata | A85 imbalance and A81 contracted reserves. Selected and pinned. |
| [esett-py](https://pypi.org/project/esett-py/0.1.0/) | 0.1.0 | 2026-09-20 | MIT SPDX expression | `Client.prices.values()` wraps EXP14/Prices; selected and pinned. No reserve-capacity endpoint. |
| [fingrid-py](https://pypi.org/project/fingrid-py/0.2.0/) | 0.2.0 | 2026-02-10 | UNKNOWN: registry and wheel supply no licence declaration/file | Generic dataset access, not a Nordic market normalizer. Can request FI imbalance dataset 319; no Fingrid frozen sets are in this packet. Pinned for reproducible inventory, not timed. |
| [clarigrid](https://pypi.org/project/clarigrid/0.2.0/) | 0.2.0 | 2026-07-29 | Apache-2.0, wheel licence and classifier | Fingrid `get_imbalance_prices()` maps dataset 319. Energinet adapter has spot/load/generation functions, no imbalance or reserve-capacity function. Pinned for inventory; no equivalent frozen Fingrid workload. |
| [pyfingridapi](https://pypi.org/project/pyfingridapi/0.1.1/) | 0.1.1 | 2024-11-23 | MIT, wheel licence/classifier | Generic Fingrid series API; no matching frozen workload. Current maintenance unestablished; not selected. |
| [datacollectors](https://pypi.org/project/datacollectors/0.7.35/) | 0.7.35 | 2025-10-10 | MIT classifier | Inspected EDS adapter exposes gas balancing, day-ahead, production/consumption, transmission and forecasts. No electricity imbalance/reserve-capacity function: not comparable. |
| [ET-eds-api](https://pypi.org/project/ET-eds-api/0.2.0/) | 0.2.0 | 2026-06-05 | MIT, wheel licence | Inspected `fetcher_p` / `fetcher_ve`: spot-price and production/capacity-per-municipality helpers. Installed capacity is not procured reserve capacity. Not comparable. |

Source inspection used the exact PyPI wheels, not an unpinned repository branch. No dedicated
Svenska kraftnat historical imbalance/reserve-capacity Python client was identified in this
bounded PyPI search. This is not proof that none exists. mFRR bid-submission libraries are a
different workload. The survey is not an exhaustive market census.


## Runtime, inputs and equivalence

Nordic Balancing package metadata is 0.3.0; library sources are unchanged from `74d61e6`, including
its 0.3.1 EDS DST fix. This is not a benchmark of the concurrent optimization branch. Most bodies
were recorded with `29bcbe2`; per-set recorder commits remain in the results. The actual same bytes
were replayed for both clients. All 12 sets, 847 exchanges and 214,803,118 body bytes validated;
before/after response-body hashes and set metadata were identical.

The 24-hour UTC day and the added Swedish local day are separate sets. The local day spans
**2025-10-25T22:00Z to 2025-10-26T23:00Z**, with 100 quarters. October and year labels denote
the exact UTC windows recorded in `set.json`, not an implicit local-calendar range.

Both A85 clients run sequentially, with Nordic `max_workers=1`; entsoe-py uses its public
`EntsoePandasClient.query_imbalance_prices()` per zone. The latter returns native-resolution
DataFrames per zone; Nordic returns a time/zone-sorted dataclass list, or retrieval plus its public
`to_frame()`. No validation bypass or raw-disabled fast path is introduced. Yearly A85 outputs
have different cardinalities because Nordic expands published hours to quarters. Native records/s
is therefore not an equal-work throughput ratio on those windows.

Both eSett clients call EXP14/Prices. esett-py uses the public `max_window=31 days` option to match
Nordic's fixed 31-day windows; its default is 366 days. This is a disclosed common-request
configuration, not a default-mode performance claim. Its native output is `list[dict]`, not a
DataFrame. Nordic frame timings for eSett are extra conversion-cost evidence, not a paired
DataFrame comparison.

**Request mappings: none.** Secret query fields are stripped and parameter names are stably sorted
as FIXTURES.md requires. Host, path, parameter spelling, periods, area and repeated-value order
are preserved. Unrecorded keys raise. httpx, requests and urllib adapters serve exact status,
headers and bytes; socket connections are additionally blocked inside workers.

For A81, entsoe-py requests January-to-February with `offset=0` and
`type_MarketAgreement.Type=A01`. The corpus contains Nordic's seven-day/adaptively split requests
with `Type_MarketAgreement.Type=A01`. The first complete requested period is absent, so even a
parameter-case alias could not establish an identical request. No ZIPs were combined or windows
rewritten. Nordic replayed 175,200 records; competitor capacity values remain NOT_COMPARABLE.

EDS has no equivalent selected-client electricity imbalance/reserve function. Fingrid and SvK
have no frozen workload in this packet. Coverage limitations are not claims that their APIs or
generic dataset downloaders cannot retrieve the underlying data.

## Correctness results

Alt means entsoe-py for A85/A81 and esett-py for eSett. A missing quarter is relative to a complete
15-minute query grid; native hourly output is not supposed to populate that grid. Every compared
output had zero duplicate intervals, zero per-zone reorderings, zero unexpected timestamps and
zero null/category-column mismatches. Numeric differences are EUR/MWh on common timestamps.

| Workload | Nordic rows | Alt rows | Nordic missing quarters | Alt missing quarters | Nordic-only / alt-only rows | Max price difference |
|---|---:|---:|---:|---:|---:|---:|
| A85 SE3 UTC day | 96 | 96 | 0 | 0 | 0/0 | 0.0 |
| A85 SE3 local DST day | 100 | 100 | 0 | 0 | 0/0 | 0.0 |
| A85 SE3 Oct 2025 UTC | 2976 | 2976 | 0 | 0 | 0/0 | 0.0 |
| A85 SE3 2025 | 35020 | 29563 | 20 | 5477 | 5457/0 | 0.0 |
| A85 all 12 zones 2025 | 413119 | 358708 | 7361 | 61772 | 54411/0 | 0.0 |
| A85 SE3 2022 | 34724 | 8681 | 316 | 26359 | 26043/0 | 0.0 |
| A81 NO FCR-N 2025 | 175200 | - | 0 | - | - | - |
| eSett SE3 2025 | 35040 | 35040 | 0 | 0 | 0/0 | 0.0 |
| eSett all 12 zones 2025 | 420480 | 420480 | 0 | 0 | 0/0 | 0.0 |
| EDS DK imbalance 2025 | - | - | - | - | - | - |
| EDS FCR-N 2025 | - | - | - | - | - | - |
| EDS aFRR Oct 2025 | - | - | - | - | - | - |

First/last starts below are actual returned observations, not assumed window endpoints.
All-zone rows show global extrema; per-zone endpoints and first missing starts are in
`compare-real.json`. A native hourly last start can precede the normalized last start by
45 minutes while covering the same final published hour.

| Workload | Nordic first / last UTC | Alt first / last UTC |
|---|---|---|
| A85 SE3 UTC day | 2025-10-26T00:00:00Z / 2025-10-26T23:45:00Z | 2025-10-26T00:00:00Z / 2025-10-26T23:45:00Z |
| A85 SE3 local DST day | 2025-10-25T22:00:00Z / 2025-10-26T22:45:00Z | 2025-10-25T22:00:00Z / 2025-10-26T22:45:00Z |
| A85 SE3 Oct 2025 UTC | 2025-10-01T00:00:00Z / 2025-10-31T23:45:00Z | 2025-10-01T00:00:00Z / 2025-10-31T23:45:00Z |
| A85 SE3 2025 | 2025-01-01T00:00:00Z / 2025-12-31T23:45:00Z | 2025-01-01T00:00:00Z / 2025-12-31T23:45:00Z |
| A85 all 12 zones 2025 | 2025-01-01T00:00:00Z / 2025-12-31T23:45:00Z | 2025-01-01T00:00:00Z / 2025-12-31T23:45:00Z |
| A85 SE3 2022 | 2022-01-01T00:00:00Z / 2022-12-31T23:45:00Z | 2022-01-01T00:00:00Z / 2022-12-31T23:00:00Z |
| eSett SE3 2025 | 2025-01-01T00:00:00Z / 2025-12-31T23:45:00Z | 2025-01-01T00:00:00Z / 2025-12-31T23:45:00Z |
| eSett all 12 zones 2025 | 2025-01-01T00:00:00Z / 2025-12-31T23:45:00Z | 2025-01-01T00:00:00Z / 2025-12-31T23:45:00Z |

## Independent source coverage and difference classification

`source_audit.py` decodes the frozen XML independently with ElementTree, reconstructs A01/A03
publication slots, and expands source hours only within their Period boundaries. It does not call
Nordic's parser or fill across missing Periods. Its tests cover gaps, changing A03 prices and
explicit unpublished values. All six A85 sets and both representations match it exactly:

| A85 workload | Native source intervals | Source-supported quarters | Source observations dropped by either client | Source price difference |
|---|---:|---:|---:|---:|
| A85 SE3 UTC day | 96 | 96 | 0 | 0 |
| A85 SE3 local DST day | 100 | 100 | 0 | 0 |
| A85 SE3 Oct 2025 UTC | 2976 | 2976 | 0 | 0 |
| A85 SE3 2025 | 29563 | 35020 | 0 | 0 |
| A85 all 12 zones 2025 | 358708 | 413119 | 0 | 0 |
| A85 SE3 2022 | 8681 | 34724 | 0 | 0 |

* **Expected domain difference — hourly expansion:** SE3 2025 has 1,819 source hours and
  27,744 native quarters. Expansion adds 5,457 starts. The frozen source changes from PT60M
  to PT15M at **2025-03-18T00:00Z**. All-zone 2025 adds 54,411 starts; SE3 2022 adds
  26,043. None are fabricated prices or omitted native observations.
* **Expected domain difference — calendar grid versus source coverage:** the frozen source itself
  lacks 20 SE3 quarters in 2025, 7,361 aggregate zone-quarters in all-zone 2025, and 316 SE3
  quarters in 2022. Both clients preserve exactly the available source observations. The cause
  of those publication gaps is UNKNOWN; absence alone does not establish an upstream defect.
* **Expected domain difference — representation:** local-aware DataFrames, UTC dataclasses, raw
  dictionaries, source-resolution retention, global versus per-zone ordering and raw metadata
  are different public contracts. Their common price values and UTC identities agree.
* **Expected domain difference — request/coverage limitation:** A81 chunking/parameters cannot be
  replayed from identical keys, and the selected EDS clients lack equivalent functions.

No real-payload implementation defect or price discrepancy was found in the checked features.
`compare-classification.json` records the final per-workload classifications separately from
raw measurement statuses, which intentionally remain MEASURED_REQUIRES_CLASSIFICATION.

| Output | Source/raw retention | Categories / direction / resolution |
|---|---|---|
| Nordic dataclasses | Source attribution and independent raw-record snapshots | A85 categories, curve, namespace, raw flow and financial-price fields retained; source resolution explicit. A85 flow direction is not promoted to a verified dominating direction. |
| Nordic `to_frame()` | Typed columns, source and resolution; raw omitted | Normalized price/direction columns. Original dataclasses are retained during conversion measurement. |
| entsoe-py A85 frames | No raw XML or source-attribution column | A04/A05 pivot to Long/Short; zone-aware local index. Raw flow/financial fields and explicit source-resolution metadata are absent. |
| esett-py dictionaries | Original JSON fields retained | Raw time, MBA, prices and direction field; no typed market-record normalization or hourly expansion. |

## Performance results

Medians below are **client-side processing with replayed transport**, not network/API latency or
XML-parser-only timings. All samples, min/max, CPU, peak RSS, throughput and conversion components
are retained in `compare-real.json`. No p95 is estimated for N < 20; all current p95 fields are null.
Rows/s counts native output rows and must not be compared as equal-work rates across hourly
versus quarter-hour output. eSett Nordic-frame rows are unpaired conversion-cost measurements.

| Workload | Mode | N | Wall s | CPU s | Peak MiB | Rows/s | Frame component s |
|---|---|---:|---:|---:|---:|---:|---:|
| A85 SE3 UTC day | nb-records | 7 | 0.0249 | 0.0312 | 32.1289 | 3852.3274 | - |
| A85 SE3 UTC day | nb-frame | 7 | 0.0655 | 0.0312 | 73.5859 | 1465.0740 | 0.0049 |
| A85 SE3 UTC day | entsoe-py | 7 | 0.1262 | 0.0625 | 79.6055 | 760.4376 | - |
| A85 SE3 local DST day | nb-records | 7 | 0.0205 | 0.0156 | 32.1562 | 4879.4531 | - |
| A85 SE3 local DST day | nb-frame | 7 | 0.0441 | 0.0156 | 73.5039 | 2267.2549 | 0.0064 |
| A85 SE3 local DST day | entsoe-py | 7 | 0.2232 | 0.0781 | 79.4844 | 448.1060 | - |
| A85 SE3 Oct 2025 UTC | nb-records | 7 | 0.7280 | 0.4375 | 43.1602 | 4088.1698 | - |
| A85 SE3 Oct 2025 UTC | nb-frame | 7 | 1.0748 | 0.3906 | 84.9492 | 2768.9264 | 0.0175 |
| A85 SE3 Oct 2025 UTC | entsoe-py | 7 | 2.7938 | 1.5312 | 113.0898 | 1065.2137 | - |
| A85 SE3 2025 | nb-records | 3 | 8.2335 | 4.7656 | 139.6836 | 4253.3511 | - |
| A85 SE3 2025 | nb-frame | 3 | 7.1321 | 4.5938 | 181.2930 | 4910.1742 | 0.1533 |
| A85 SE3 2025 | entsoe-py | 3 | 25.9367 | 14.2656 | 384.3320 | 1139.8133 | - |
| A85 all 12 zones 2025 | nb-records | 3 | 87.1244 | 54.6094 | 641.2461 | 4741.7149 | - |
| A85 all 12 zones 2025 | nb-frame | 3 | 84.5561 | 51.9531 | 776.9453 | 4885.7396 | 2.0687 |
| A85 all 12 zones 2025 | entsoe-py | 3 | 238.3883 | 157.3750 | 472.2695 | 1504.7212 | - |
| A85 SE3 2022 | nb-records | 3 | 2.7067 | 1.4219 | 83.1797 | 12829.0615 | - |
| A85 SE3 2022 | nb-frame | 3 | 2.9260 | 1.5156 | 133.0508 | 11867.1999 | 0.1119 |
| A85 SE3 2022 | entsoe-py | 3 | 3.8093 | 2.2656 | 171.6133 | 2278.9183 | - |
| A81 NO FCR-N 2025 | nb-records | 3 | 9.1459 | 5.5938 | 209.8633 | 19156.0264 | - |
| A81 NO FCR-N 2025 | nb-frame | 3 | 9.9535 | 6.4531 | 252.3906 | 17601.9234 | 0.4802 |
| eSett SE3 2025 | nb-records | 3 | 1.0445 | 0.5938 | 88.6133 | 33547.3059 | - |
| eSett SE3 2025 | nb-frame | 3 | 1.3546 | 0.7969 | 138.3789 | 25868.2588 | 0.1439 |
| eSett SE3 2025 | esett-py | 3 | 0.1178 | 0.0938 | 71.2070 | 297447.7556 | - |
| eSett all 12 zones 2025 | nb-records | 3 | 17.1736 | 10.6094 | 700.1289 | 24484.1381 | - |
| eSett all 12 zones 2025 | nb-frame | 3 | 23.7178 | 17.2500 | 840.1875 | 17728.4673 | 2.1929 |
| eSett all 12 zones 2025 | esett-py | 3 | 1.7114 | 1.5625 | 526.2031 | 245687.8274 | - |

**Where Nordic wins:** lower measured A85 query-processing medians on all six frozen A85 sets,
also with `to_frame()`. The UTC day, local DST day and October have identical interval cardinality;
yearly results preserve identical source coverage but have the documented frequency differences.
For the exact-cardinality October case, frame-path median is 1.075 s versus 2.794 s for entsoe-py.

**Where Nordic loses:** all-zone A85 peak RSS is 641 MiB for records / 777 MiB with frame versus
472 MiB for entsoe-py. Output cardinality and retained metadata differ. On eSett, esett-py raw
retrieval is faster: SE3 0.118 s versus Nordic records 1.044 s; all zones 1.711 s versus 17.174 s.
It also uses less peak RSS. This is a raw-retrieval advantage, not proof of equivalent normalized
validation guarantees; no validation was disabled to create either result. Individual costs of
validation, normalization and raw copying were not profiled by this packet.

**Untested / non-comparable:** competitor A81 field/value parity, selected-client EDS workloads,
Fingrid/SvK frozen workloads, live transfer/latency/rate-limit performance, Linux/Docker RSS,
and identical normalized-quarter DataFrame output from entsoe-py. No universal winner or release
approval follows from these measurements.

## Method and limitations

* Each measurement uses a fresh Python subprocess with only the relevant client libraries loaded.
  Bodies and clients are prepared before timing, then one warmup is discarded, its output released
  and garbage collected, and one public call is timed. Outputs must match across repeats. Short
  windows have N=7; years N=3. All calls/runs are serial. A first attempt that imported every
  competitor into every worker was discarded before the reported run.
* Client imports/construction, fixture loading/hashing, correctness audits and JSON serialization
  are excluded from wall/CPU timings. Replay response construction, request building, decoding,
  parsing, normalization and requested frame conversion are included. Native row auditing uses
  retrieved records; unchanged `to_frame()` behavior is also covered by the repository suite.
* RSS uses psutil Windows `peak_wset`, a process-lifetime high-water mark including imports,
  setup, loaded bodies and warmup. It is captured before correctness auditing/serialization and
  is not incremental parser allocation. Non-Windows peak RSS currently fails explicitly.
* Host: Windows 11 build 22621, Intel i5-9400F @2.90GHz, six physical/logical CPUs, about 32 GiB RAM,
  affinity CPUs 0–5; CPython 3.12.13, pandas 3.0.6, httpx 0.28.1, psutil 7.2.2. Versions,
  processor signature, RAM, affinity, Git commit and measured comparison-code hash are recorded.
  No Docker/container CPU or memory caps were imposed. This is an unisolated local-host run.
* Host scheduling/background activity produces visible wall/CPU variation. Mode order is grouped,
  not randomized. N=3 years and N=7 short windows support descriptive medians, not tail or portable
  performance claims. Windows CPU timer granularity especially limits short-call interpretation.
  Do not infer conversion cost by subtracting independent mode medians; use the frame component.

## Regression evidence and reproduction

The 17 focused tests pass, including replay misses, byte/status equality, DST, native resolution
changes, gaps, duplicates, nulls and the independent audit. Earlier handmade evidence in
`compare-smoke.json` is functional regression evidence from the preparatory commit, not real-market
or current-method performance evidence. Injected duplicate positions are an upstream-format defect
in the handmade input: Nordic rejects them while entsoe-py overwrites them. Missing optional A85
category/amount elements expose a pinned entsoe-py AttributeError (implementation defect relative
to the schema-optional contract documented in this checkout). Neither issue was observed in the
real corpus. No actual upstream publication defect is inferred from those injected tests.

```powershell
uv sync --locked --group benchmarks-compare
New-Item -ItemType Directory -Force benchmarks/.tmp | Out-Null
uv run --locked --group benchmarks-compare pytest benchmarks/tests -q -p no:cacheprovider --basetemp benchmarks/.tmp/tests
uv run --locked --group benchmarks-compare python -m benchmarks.compare
uv run --locked --group benchmarks-compare python -m benchmarks.source_audit
$LASTEXITCODE
```

The full measurement command is intentionally long for the all-zone year. Shared fixtures stay
external and are never modified or bundled. `compare-real.json` contains measurements and exact
missing keys; `compare-source.json` contains independent coverage; `compare-classification.json`
contains final interpretation; `compare-candidates.json` preserves the registry survey.
