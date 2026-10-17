# Competitor comparison: fixture gate pending

Status: **BLOCKED for real-market correctness and performance results**. On 2026-10-17 the
shared directory `C:\Docker\nb-perf-fixtures` had no `READY` marker. Partial sets are not a
frozen corpus. No provider was contacted by this packet, and no shared fixture was changed.
`results/compare-real.json` records every required workload as NOT_EXECUTED.
There is no supported performance winner or loser yet.

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

## Comparable windows and request policy

* A85 SE3 day/month/year and all-zone year: potential entsoe-py comparisons. Both must replay
  the identical request keys and body bytes. Multiple zones use each library's public calls;
  requests are sequential in both, with Nordic Balancing `max_workers=1`.
* Historical A85 hourly data: native output comparison only. Nordic Balancing emits four
  quarters while retaining source resolution. entsoe-py emits native hourly rows. No fill or
  upsampling was added to entsoe-py. Records/s therefore has a different denominator and cannot
  support a speed ranking on those windows.
* A81 FCR-N: entsoe-py has a corresponding contracted-reserves function, but uses month chunks,
  `type_MarketAgreement.Type` and `offset=0`. Nordic Balancing uses at most seven-day chunks
  and `Type_MarketAgreement.Type`. The harness preserves these requests. They may miss the
  frozen manifest, in which case the result is NOT_COMPARABLE, not a fabricated match.
  Capacity field/category correctness is untested until exact requests can be replayed.
* eSett: both call EXP14/Prices. esett-py's public `max_window` is configured to 31 days to
  match Nordic Balancing's fixed public behaviour; its default is 366 days. This configuration
  is disclosed, and no default-mode performance claim is made. MBA query ordering, timestamps,
  zone sets and chunk boundaries must still match exactly.
* EDS imbalance, FCR-N and aFRR: no equivalent endpoint in the selected comparison clients.
  NOT_COMPARABLE. Fingrid/SvK: no frozen workload here; NOT_EXECUTED.

**Request mappings: none.** The replay key strips secret query fields and sorts parameter names
as specified by FIXTURES.md. It does not change host/path, parameter spelling, periods, area,
chunking or values; repeated query values retain order. Unknown keys raise `ReplayMiss`.
Both `httpx` and `requests` adapters preserve status, headers and exact body bytes, with no
fallback network transport. The eSett adapter patches only `urllib.request.urlopen`, retaining
esett-py's JSON decoding. Worker socket connections are blocked as an additional guard.

The provisional `entsoe-imb-SE3-day-2025-10-26/set.json` describes **2025-10-26T00:00Z to
2025-10-27T00:00Z**, yielding 96 quarters. That is a 24-hour UTC day. Sweden's 25-hour local
2025-10-26 is **2025-10-25T22:00Z to 2025-10-26T23:00Z**. This is a fixture-coverage issue,
not an observed upstream data defect. The shared fixture was not changed or benchmarked.
The real corpus must include the full local day before claiming this edge case is covered.

## Correctness before timing

`compare.py` keeps native interval frequency and order. It records count, first/last UTC instant,
expected quarter-hour grid, gaps, unexpected intervals, duplicates, per-zone order, nulls, value
columns and available source resolutions. Pairwise comparison records missing intervals,
null/category-column mismatches and maximum absolute price difference on matching instants.
Missing quarters in hourly native output are a domain difference, not silently called data loss.
No winner is assigned automatically, even when all numerical comparisons agree.

The following are **handmade offline regression evidence**, not real-provider measurements.
`results/compare-smoke.json` stores the actual results; `benchmarks/tests/test_compare.py`
constructs small sets in the exact manifest/body/set format without committing body files.

| Case | Nordic Balancing | entsoe-py | Difference classification and evidence |
|---|---|---|---|
| 25-hour Swedish DST window, explicit PT15M points | 100 records; UTC, ordered, no gaps/duplicates | Same 100 instants; Europe/Stockholm native index converts to matching UTC | No difference; max absolute price difference 0. |
| Hourly then quarter-hour Periods in one document | 8 quarters, resolutions 3600/900 seconds retained | 5 native rows (one hourly, four quarters) | Expected domain difference; 3 extra normalized quarters, matching prices differ by 0. Synthetic boundary, not proof of the real 2025 transition. |
| Repeated same point position | SourceError: `entsoe: duplicate price for category A04` | Repeated key is overwritten, 4 output rows | Upstream-format defect injected by the test. Validation-policy difference; no claim that real upstream data contains it. entsoe-py's `_parse_timeseries_generic` stores points in a dict. |
| Omitted optional category or price amount | Accepts absent category / retains unpublished null amount | AttributeError during element `.text` access | Implementation defect relative to optional A85 elements documented in this checkout's schema-backed parser. Limited to the exact pinned parser and handmade case. |
| eSett single quarter | One normalized record with source/raw metadata | One original dict | Matching instant and price, max difference 0; full source fields kept by eSett and native Nordic records. |
| All required real sets | NOT_EXECUTED | NOT_EXECUTED | READY gate absent. No upstream defect or real implementation defect inferred. |

Nordic Balancing dataclasses keep source attribution, source resolution, raw categories,
flow direction and financial-price fields. A85 flow direction remains raw: it is not mapped
into a verified dominating direction. `to_frame()` retains typed columns but omits raw.
entsoe-py's imbalance DataFrame pivots A04/A05 categories to Long/Short columns, uses a local
zone-aware index and drops source XML/flow/financial metadata. eSett-py preserves raw source
dictionaries; normalization, strict validation, expansion and attribution are additional work
in Nordic Balancing. Performance equivalence must account for these differences.

## Performance status and method

| Workload | N / result | Legitimate speed conclusion |
|---|---|---|
| Real A85 day/month/year/all zones, historical hourly | NOT_EXECUTED | Untested |
| Real A81 capacity | NOT_EXECUTED; request compatibility unresolved | Untested |
| Real eSett year | NOT_EXECUTED | Untested |
| EDS workloads | NOT_COMPARABLE with selected clients | None |
| Handmade DST harness smoke | 7 measured subprocesses per mode, each after one warmup | Functional verification only; no market performance claim |

Each measured run starts a fresh Python subprocess, imports clients and preloads frozen bodies,
performs one warmup, releases its output and collects garbage, then measures one public client
call. Repeated results must be identical. Minimum N is 7 for short windows and 3 for years.
CPU is `time.process_time()`, wall time is `perf_counter()`, and throughput is native output rows/s.
Nordic Balancing is measured separately as dataclass output and retrieval plus `to_frame()`;
frame conversion time is also reported as a component. Both libraries retain native metadata
behaviour. No raw-disabled fast path or validation bypass is used.

Median/min/max and all samples are retained. p95 is null for N < 20: seven or three samples do
not give a defensible tail estimate. Increase `--repeats 20` for an interpolated descriptive p95.
Windows CPU timer granularity limits interpretation of very short calls.

Peak RSS uses psutil's Windows `peak_wset`: it is the process-lifetime high-water mark, including
imports, loaded fixture bodies, setup and warmup. It is not incremental parsing memory. RSS is
captured before output auditing/serialization. The current implementation fails explicitly on
platforms without `peak_wset`; Linux/Docker RSS sampling is not implemented. No Docker run or
CPU/memory cgroup limit was established by this packet. Environment, affinity, host memory,
package versions and Git commit are recorded in compare-real.json. Local timings depend on
other host activity and do not measure API latency or transfer time.

## Reproduction and next gate

From this worktree in PowerShell (Python 3.12.13 used here):

```powershell
uv sync --locked --group benchmarks-compare
New-Item -ItemType Directory -Force benchmarks/.tmp | Out-Null
uv run --locked --group benchmarks-compare pytest benchmarks/tests -q -p no:cacheprovider --basetemp benchmarks/.tmp/tests
uv run --locked --group benchmarks-compare python -m benchmarks.smoke
uv run --locked --group benchmarks-compare python -m benchmarks.compare
$LASTEXITCODE
```

The last command writes BLOCKED_FIXTURES_NOT_READY without consuming partial sets until READY
exists. After READY, it reads bodies in place, never copies them into Git, and writes results.
Any request mismatch must be investigated from recorded missing keys; no broad matching is allowed.
Every real difference still needs source-level classification before a ranking is published.
Use a new result filename via `--output benchmarks/results/compare-<label>.json` to retain runs.

**Wins:** handmade evidence supports additional validation/metadata coverage, not a speed win.
**Losses:** no real speed loss measured; extra normalization/raw retention adds work that must be measured.
**Untested:** every real-workload performance claim, real hourly-transition handling, A81 field parity,
and the complete local DST day. Packet B is not accepted as a completed competitor benchmark yet.
