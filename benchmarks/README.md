# Benchmarks

How fast `nordic-balancing` is, how it compares with other Python clients for the same data,
and the scripts that measure it.

## How it is measured

Real provider responses were recorded once and are replayed byte for byte through each
client's public `http=` parameter. Every client therefore parses exactly the same input, and
network time is left out. Each measurement runs in a fresh Python process after one warm-up
run, and the tables show the median of 3 to 7 runs. The machine is a Windows 11 desktop
(Intel i5-9400F, 6 cores, 32 GiB, Python 3.12). It is not a dedicated benchmark machine, so
repeated runs of the same code can differ by 10 % or more.

## Compared with other clients

Same recorded responses for both clients, measured on 2026-10-20 with version 0.4.0. Times are
wall-clock seconds; memory is peak resident memory.

| Workload | nordic-balancing | Other client | Prices identical |
|---|---:|---:|---|
| ENTSO-E imbalance, SE3, October 2025 | 0.18 s / 45 MiB | entsoe-py 0.8.1: 1.42 s / 116 MiB | yes, 2,976 of 2,976 |
| ENTSO-E imbalance, SE3, 2025 | 2.2 s / 143 MiB | entsoe-py: 13.8 s / 388 MiB | yes, on every common interval |
| ENTSO-E imbalance, 12 zones, 2025 | 30 s / 659 MiB | entsoe-py: 163 s / 474 MiB | yes, on every common interval |
| eSett imbalance, SE3, 2025 | 0.39 s / 90 MiB | esett-py 0.1.0: 0.11 s / 72 MiB | yes, 35,040 of 35,040 |
| eSett imbalance, 12 zones, 2025 | 4.8 s / 672 MiB | esett-py: 1.4 s / 527 MiB | yes, 420,474 of 420,474 (see below) |

- **ENTSO-E:** nordic-balancing is 4.4 to 10 times faster than entsoe-py on all six recorded
  workloads (1.4 to 3.8 times in version 0.3.0). It uses more memory for a full year of all
  12 zones.
- **eSett:** esett-py is 3.4 to 3.6 times faster (about 10 times in version 0.3.0), because it
  does much less. It decodes the JSON and returns the raw dictionaries. nordic-balancing
  decodes the same JSON, then checks every row against the
  [data contracts](../README.md#data-contracts) and builds a typed record for each interval.
  Decoding takes about one seventh of our time on the SE3 year; the checks and records take the
  rest.
- **The 6 eSett prices that differ** are Danish intervals that were not settled yet. eSett
  publishes them with every price set to `0.0`; nordic-balancing returns `None` for them and
  esett-py returns `0.0`.
- **ENTSO-E row counts differ on yearly windows** because nordic-balancing splits each
  hourly price into four quarter-hour records (SE3 2025: 35,020 rows against 29,563). An
  independent XML decoder (`source_audit.py`) confirmed that neither client dropped or changed
  a published value.
- Energi Data Service, Fingrid and Svenska kraftnät have no other maintained Python client for
  this data, so they are measured on their own.

## How it got faster

Each change below was checked the same way: the output of all 12 recorded workloads (1.42
million records, source data included) is byte-identical before and after, and before and
after were timed alternately on the same machine.

### Copying source data (version 0.3.1, 2026-10-18)

Every record keeps its own copy of the source row in `raw`. Making that copy was the largest
cost for every provider, and most of it was spent deciding how to copy each value rather than
copying it. Copying plain JSON values directly made year-long queries 19 to 36 % faster. Peak
memory did not change.

| Workload (2025) | 0.3.0 | 0.3.1 | change |
|---|---:|---:|---:|
| eSett SE3 | 1.22 s | 0.79 s | -36 % |
| eSett, 12 zones | 15.8 s | 10.9 s | -31 % |
| ENTSO-E SE3 | 4.30 s | 3.40 s | -21 % |
| ENTSO-E, 12 zones | 60.0 s | 48.4 s | -19 % |
| Energi Data Service DK | 1.75 s | 1.38 s | -21 % |

### eSett parsing (version 0.4.0, 2026-10-20)

The eSett parser stopped repeating work for every row:

- a timestamp shared by all 12 zones is parsed once instead of once per zone;
- the five numbers in a settled row are checked in one step when they are all ordinary numbers,
  with the slower column-by-column check (and its error messages) kept for anything unusual;
- records are built without repeating checks the parser has just done, and without copying
  the source row a second time. A row that feeds several records (the four quarters of an
  hourly price) is still copied for each of them, so no two records share data.

| Workload (2025) | before | after | change |
|---|---:|---:|---:|
| eSett SE3 | 0.85 s | 0.45 s | -47 % |
| eSett, 12 zones | 11.8 s | 5.3 s | -55 % |

Peak memory is unchanged for SE3 (127 MiB) and 3 % lower for 12 zones (689 to 669 MiB).

### ENTSO-E and Energi Data Service parsing (version 0.4.0, 2026-10-20)

Profiling every workload showed the same kind of repeated work:

- ENTSO-E searched each XML element's children again for every field it read, which was a
  third of the time on FCR-N capacity. It now indexes them once per element.
- ENTSO-E publishes most imbalance prices twice, once per price category, and the parser built
  each of those records three times. It now builds each record once and checks the second
  publication against it, with the same rules and the same errors.
- Energi Data Service copied each capacity row once for every record made from it (eight
  times for aFRR and mFRR) and parsed the same timestamps again on every row. It now copies
  only where records would otherwise share data, and parses each timestamp once per query.

Besides the byte-identical check, 15,000 generated test documents (duplicate categories, gaps,
conflicting directions, invalid numbers, the October clock change) gave the same records and
the same errors before and after.

| Workload | before | after | change |
|---|---:|---:|---:|
| ENTSO-E imbalance SE3, October 2025 | 0.33 s | 0.18 s | -44 % |
| ENTSO-E imbalance SE3, 2025 | 3.47 s | 2.17 s | -37 % |
| ENTSO-E imbalance SE3, 2022 (hourly) | 2.07 s | 0.90 s | -57 % |
| ENTSO-E imbalance, 12 zones, 2025 | 51.6 s | 30.0 s | -42 % |
| ENTSO-E FCR-N capacity NO, 2025 | 8.36 s | 5.69 s | -32 % |
| Energi Data Service imbalance DK, 2025 | 1.38 s | 0.96 s | -30 % |
| Energi Data Service aFRR capacity, October 2025 | 0.66 s | 0.51 s | -23 % |
| Energi Data Service FCR-N capacity, 2025 | 6.34 s | 3.85 s | -39 % |
| eSett SE3, 2025 (no code change) | 0.45 s | 0.41 s | -11 % |

The last row is a reference: the eSett code did not change in this step, yet it timed 11 %
faster. Differences of about 10 % in this table are therefore within normal variation on this
machine. Peak memory is within 3 % of before, except Energi Data Service FCR-N, which dropped
from 388 to 360 MiB.

Fingrid and Svenska kraftnät were not changed: there are no recorded workloads for them, and
most of a Fingrid query is spent waiting, because the client waits 2 seconds between Fingrid
requests by default.

## Scripts

The recorded responses are API payloads and are not redistributed. Scripts read them from
`$NB_FIXTURES`, or from the git-ignored `benchmarks/.fixtures/` when that is unset. Result
files are written to `benchmarks/results/` and are not committed.

- `fixtures.py`: records and replays `httpx` traffic; a request that was not recorded raises.
- `workloads.py`: the client calls that are recorded and measured.
- `record.py`: records one named set of live responses (API keys come from the environment).
- `run.py`: wall time, CPU, peak memory and time per stage (transport, decoding, parsing,
  record building, merging) for each set.
- `compare_outputs.py`: writes every record as canonical JSON lines and compares two versions;
  run it before and after any optimisation.
- `compare.py`, `source_audit.py`: the comparison with other clients and the independent XML
  check.
- `Dockerfile`: a pinned container build for repeatable runs.
- `arcticgrid_reference/`: a downstream consumer replaying a recorded dataset
  ([README](arcticgrid_reference/README.md)).

```bash
uv run --group benchmarks python benchmarks/run.py --sets <set>[,<set>...] --tag <label>
uv run --group benchmarks python benchmarks/compare_outputs.py diff <labelA> <labelB>
uv run --locked --group benchmarks-compare python -m benchmarks.compare
```

Memory for a full year of all 12 zones, measured with synthetic responses, is in
[examples/benchmarks/](../examples/benchmarks/README.md).
