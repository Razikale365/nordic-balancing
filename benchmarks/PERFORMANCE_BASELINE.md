# Performance baseline — v0.3.0 (29bcbe2)

All times are **replay** measurements: recorded HTTP responses served from
`C:\Docker\nb-perf-fixtures`, so network/provider latency is excluded. The
recorded `elapsed_s` per exchange is kept in each set's `manifest.jsonl`
(`live_elapsed_s` below) and is what a live call actually paid.

Method: fresh subprocess per measurement, 1 warmup + N repeats (N=7 when a
probe child finishes under 2 s, else 3); reported values are medians.
Per-set children run two passes: (1) an instrumented **serial**
(`max_workers=1`) pass with timers on stage boundaries — the stage table,
since per-call wall under threads measures contention, not work — and
(2) an unpatched pass at default concurrency — the `wall` number, the
number a user sees. Pass-1 records are freed before pass 2. Fixture
loading happens outside every timer; `to_frame` is timed separately after
the e2e pass. Environment details (host CPU/RAM/OS, Python and package
versions, git commit, cgroup limits, Docker image) are in each JSON's
`environment` block. On Windows (no cgroup support) the runs below are
unrestricted host runs; the Docker path is verified and documents its own
limits — see `benchmarks/README.md`.

## End-to-end results (v0.3.0, host, medians)

| set | records | requests | bytes | wall s | serial s | CPU s | peak RSS | rec/s | live s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| entsoe-imb-SE3-day-2025-10-26 | 96 | 1 | 1.9 KB | 0.01 | 0.01 | 0.00 | 72 MiB | 8,808 | 3.2 |
| entsoe-imb-SE3-localday-2025-10-26 | 100 | 1 | 2.7 KB | 0.02 | 0.02 | 0.00 | 72 MiB | 6,654 | 17.9 |
| entsoe-imb-SE3-month-2025-10 | 2,976 | 1 | 36 KB | 0.41 | 0.36 | 0.30 | 86 MiB | 7,172 | 2.0 |
| entsoe-imb-SE3-year-2025 | 35,020 | 1 | 0.33 MB | 4.45 | 4.35 | 2.34 | 221 MiB | 7,872 | 8.7 |
| entsoe-imb-all-year-2025 | 413,119 | 12 | 3.7 MB | 90.39 | 78.10 | 60.41 | 1,548 MiB | 4,571 | 153.6 |
| entsoe-imb-SE3-year-2022 | 34,724 | 1 | 0.10 MB | 4.72 | 5.48 | 3.36 | 170 MiB | 7,362 | 2.6 |
| entsoe-cap-FCRN-NO-year-2025 | 175,200 | 785 | 3.5 MB | 14.02 | 16.20 | 11.27 | 386 MiB | 12,498 | 4,020.5 |
| esett-imb-SE3-year-2025 | 35,040 | 12 | 12.1 MB | 2.83 | 5.79 | 1.56 | 163 MiB | 12,370 | 7.7 |
| esett-imb-all-year-2025 | 420,480 | 12 | 145 MB | 47.69 | 54.31 | 17.83 | 1,146 MiB | 8,818 | 13.1 |
| eds-imb-DK-year-2025 | 58,086 | 6 | 18.4 MB | 5.02 | 4.75 | 2.20 | 159 MiB | 11,579 | 17.1 |
| eds-cap-aFRR-month-2025-10 | 71,424 | 1 | 2.5 MB | 5.90 | 2.50 | 1.50 | 138 MiB | 12,109 | 2.1 |
| eds-cap-FCRN-year-2025 | — | 14 | 29.1 MB | **call fails on v0.3.0** | | | | | — |

Host variance caveat: these runs shared the machine with fixture recording
and other load; repeat samples on `entsoe-imb-all-year-2025` spanned
~63–116 s. Treat the walls as magnitudes, not precise medians; the serial
stage table below is the attribution source. `eds-cap-FCRN-year-2025`
cannot be measured on v0.3.0: replaying it raises
`SourceError: HourUTC '2025-10-26T01:01:00' is not on a 15-minute boundary`
— the bug fixed by the 0.3.1 fall-back commit. Its fixture was therefore
recorded on `wp/perf` (main + fix); the JSON result for the baseline tag
records the error rather than a time.

## Stage attribution (serial instrumented pass, v0.3.0)

Shares of the serial wall. `construction` wraps the record dataclass
constructor (which runs `__post_init__` → `_copy_raw`); `dataclass_replace`
wraps `dataclasses.replace` calls (duplicate-publication merges and
hourly→quarter expansions, each re-running `__post_init__`).

Seconds per stage, with share of the serial wall in parentheses.

| set | serial s | transport | zip | decode | parse_validate | construction | replace | to_frame | residual |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| entsoe SE3 day | 0.01 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| entsoe SE3 month | 0.36 | 0.00 | 0.00 | 0.03 | 0.16 (44%) | 0.09 | 0.08 | 0.01 | 0.03 |
| entsoe SE3 2025 | 4.35 | 0.00 | 0.02 | 0.27 | 1.74 (40%) | 1.21 | 0.92 | 0.12 | 0.48 |
| entsoe all 2025 | 78.10 | 0.02 | 0.28 | 4.22 | 35.59 (46%) | 20.86 | 14.23 | 2.47 | 7.12 |
| entsoe SE3 2022 | 5.48 | 0.00 | 0.01 | 0.12 | 0.80 | 2.30 | 1.29 | 0.13 | 1.08 |
| entsoe FCR-N cap | 16.20 | 1.10 | 0.29 | 2.57 | 7.92 (49%) | 3.49 | 0.86 | 1.16 | 2.36 |
| esett SE3 2025 | 5.79 | 0.06 | 0.00 | 0.41 | 3.92 (68%) | 2.36 | 0.00 | 0.31 | 1.17 |
| esett all 2025 | 54.31 | 0.24 | 0.00 | 4.29 | 36.62 (67%) | 24.48 | 0.00 | 4.91 | 13.15 |
| eds imb 2025 | 4.75 | 0.03 | 0.00 | 0.36 | 3.57 (75%) | 1.95 | 0.00 | 0.47 | −1.12 |
| eds aFRR month | 2.50 | 0.00 | 0.00 | 0.05 | 0.00 | 1.80 | 0.00 | 0.79 | 0.75 |
| eds FCR-N year | 1.04 | 0.03 | 0.00 | 0.28 | 1.01 | 0.00 | 0.00 | 0.00 | −0.29 |

Note: for eSett/EDS `construction` is invoked inside the timed
`parse_validate` span, so those columns overlap there (which is why
`eds imb`'s residual is negative); `residual` is computed against
non-overlapping top-level stages. `eds aFRR` shows `parse_validate=0`
because EDS capacity responses go through `_fcr_capacity`, timed as
`construction`, not the imbalance row parser. `eds FCR-N` fails during
parse_validate on v0.3.0, so only its partial stages are meaningful.

## cProfile ranking (v0.3.0)

Serial profiles (`benchmarks/profile_run.py --serial`); numbers are
profiled seconds — shares, not absolutes, are the signal.

### entsoe-imb-SE3-year-2025 (124 s profiled, 35,020 records)

| share | cost |
|---|---|
| `__post_init__` cum 42.1 s (34%) | record init incl. raw copy |
| `_copy_raw` cum 37.6 s (30%) | 1.58 M recursive calls (15/record×3 constructions) |
| `dataclasses.replace` cum 33.6 s (27%) | 94,146 calls ≈ 2.7 per output record |
| `_period_points` own 22.7 s (18%) | per-Point validation loop |
| `copy.deepcopy` cum 17.2 s (14%) | per-scalar dispatch inside `_copy_raw` |
| XML feed | 2.3 s — real parsing is small |

### esett-imb-all-year-2025 (431 s profiled, 420,480 records)

| share | cost |
|---|---|
| `_parse_imbalance` cum 306 s (71%) | row → record |
| `__post_init__` cum 178 s (41%) | incl. `_copy_raw` 160 s (37%) |
| `copy.deepcopy` cum 81 s (19%) | 5.5 M scalar dispatches inside `_copy_raw` |
| `_parse_time` 24 s + `_number` 23 s + `_direction` 10 s | real parse work (13%) |
| `httpx json()` | 17.5 s decode |

### entsoe-imb-all-year-2025 (362 s profiled, all threads, 413,119 records)

1.24 M `__post_init__` and 1.13 M `replace` calls — the duplicate-merge
re-copies the merged `raw` ~2.7× per output record.

## tracemalloc peaks (v0.3.0)

| set | peak | top retained sites |
|---|---:|---|
| entsoe SE3 2025 | 98.7 MiB | `models.py:67` raw dict copies 13.4 MiB; point dicts 8.2 MiB; list copies 4.9 MiB; `dataclasses.py:1588` (replace field dicts) 4.9 MiB |
| esett all 2025 | 489 MiB | `models.py:67` raw dict copies **186 MiB (38% of traced heap)**; json decoder 129 MiB; row dicts 58 MiB; MappingProxy 16 MiB |

## memory.py synthetic-benchmark contamination

`examples/benchmarks/memory.py` builds its mock responses **inside** the
timed region. Measured share of its wall that is payload generation, not
library work (machine under recording load — walls higher than README's):

| scenario | handler s | wall s | share |
|---|---:|---:|---:|
| esett-2025 | 22.3 | 109.6 | 20% |
| esett-2022 | 5.0 | 60.8 | 8% |
| entsoe-2025 | 59.6 | 336.6 | 18% |
| entsoe-2022 | 9.7 | 173.4 | 6% |

README peak-RSS figures (1.34–2.30 GiB) remain the best memory datapoints;
the replay harness measures RSS too (per-set `peak_rss_bytes` in the JSONs).

## Bottleneck ranking (evidence → optimization order)

1. **`_copy_raw` scalar/copy dispatch** — up to 37% of serial wall across
   providers; 38% of retained heap on eSett-all. Every JSON scalar pays a
   `copy.deepcopy` dispatch; every dict pays an abc `isinstance(Mapping)`.
2. **Duplicate-publication merge via `dataclasses.replace`** (ENTSO-E
   imbalance) — ~2.7 replaces per output record, each re-deep-copying the
   accumulated `raw`. 27–45% of serial wall on ENTSO-E imbalance sets.
3. **Record construction `__post_init__` itself** — datetime checks +
   MappingProxyType; 40% cum on eSett (includes _copy_raw).
4. XML per-Point validation loop (`_period_points`) — ~18% on ENTSO-E;
   real work, higher risk to touch.

`to_frame`, decode and transport are each ≤5% everywhere; not worth it.
