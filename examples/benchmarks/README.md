# Memory benchmark

`memory.py` measures peak memory and wall time for fetching one year of
imbalance prices for all 12 bidding zones. It is fully offline: every response
is synthetic and served through `httpx.MockTransport`, so no network access and
no API keys are needed.

eSett responses are JSON arrays of records shaped like the ones in
`tests/test_esett.py`. ENTSO-E responses are ZIP bodies holding one
schema-4.4 `Balancing_MarketDocument` per zone and chunk — `curveType` A03 with
two `TimeSeries` per `Period` (categories A04 and A05 at equal prices), the
shape described under "Observed documents" in `docs/SOURCE_NOTES.md`. One
request per zone per chunk runs in parallel (`max_workers=12`, the default), so
ENTSO-E is the heavier case: XML trees, a category merge per interval and a
larger `raw` per record.

## Scenarios

| Scenario | Window | Source cadence | Records |
|---|---|---|---:|
| `esett-2025` | 2025-01-01 to 2026-01-01 | 15-minute records | 420,480 |
| `esett-2022` | 2022-01-01 to 2023-01-01 | hourly records expanded to quarters | 420,480 |
| `entsoe-2025` | 2025-01-01 to 2026-01-01 | PT15M points, categories A04+A05 | 420,480 |
| `entsoe-2022` | 2022-01-01 to 2023-01-01 | PT60M points, categories A04+A05 | 420,480 |

## Running

```
uv run --with psutil python examples/benchmarks/memory.py
```

Each scenario runs in a fresh subprocess three times and the median is
reported. Pass scenario names to run a subset, e.g.
`memory.py entsoe-2025`. The script needs `psutil` (not a project dependency)
and `pandas` for `to_frame()` (installed by `uv sync` from the dev group).

## Measures

- **peak RSS** — largest resident set of the child process in MiB, including
  the interpreter and pandas. Measured with psutil (kernel peak working set on
  Windows, `ru_maxrss` on Unix, a 10 ms sampler as fallback) because
  tracemalloc misses allocations made inside expat, the C XML parser.
- **traced peak** — `tracemalloc` peak in MiB during the fetch call; it
  excludes expat and other C allocations.
- **retained** — `tracemalloc` current size in MiB right after the call, while
  the result list is still referenced.
- **wall s** — seconds for the fetch call, including generation of the
  synthetic responses inside the mock handler.
- **frame +peak / +retained** — extra traced MiB for `to_frame(records)`; the
  lazy pandas import is warmed up first so it is not counted.
- **B/record** — peak RSS divided by the number of records returned.

## Results (run 2026-10-15)

Machine: Windows 11 (10.0.22621), 6 CPUs, 31.9 GiB RAM, Python 3.12.13.
No test runs were active; a browser was open in the background
(system CPU 6-29 %). Medians of three fresh subprocesses per scenario. Memory
figures are within 3 % of an earlier run under load on 2026-10-14; the eSett
wall times were lower here, the ENTSO-E ones were not (ENTSO-E is CPU-bound
XML parsing).

| Scenario | Records | Wall s | Peak RSS MiB | Traced peak MiB | Retained MiB | Frame +peak MiB | Frame +retained MiB | B/record |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| esett-2025 | 420,480 | 78.0 | 1,344 | 401 | 330 | 141 | 39 | 3,352 |
| esett-2022 | 420,480 | 44.4 | 816 | 274 | 237 | 141 | 39 | 2,034 |
| entsoe-2025 | 420,480 | 266.5 | 2,301 | 906 | 492 | 141 | 39 | 5,739 |
| entsoe-2022 | 420,480 | 159.2 | 1,515 | 567 | 449 | 141 | 39 | 3,778 |

## Threshold check

The flags below are the benchmark's own rule. The decision taken on these numbers
(keep the synchronous list API) and its reasoning are in
`docs/SOURCE_NOTES.md`, "Memory".

Threshold: 1.5 GiB peak RSS or 3 KiB per record. Per scenario:

- `esett-2025`: **crossed** — bytes/record (3,352 > 3,072); peak RSS stays
  under (1,344 MiB).
- `esett-2022`: not crossed (816 MiB, 2,034 B/record).
- `entsoe-2025`: **crossed** — both measures (2,301 MiB > 1,536 MiB and
  5,739 > 3,072 B/record).
- `entsoe-2022`: **crossed** — bytes/record (3,778 > 3,072); peak RSS stays
  under (1,515 MiB).

Observations, without proposing a fix: the gap between peak RSS and traced
peak is largest for `entsoe-2025` (~1.4 GiB), which is where expat's C-level
element trees live; traced retained is roughly half the traced peak there,
the other half being transient parse objects. `to_frame()` adds about
140 MiB of traced peak and 39 MiB retained on top of the records in every
scenario, independent of source.

The script is checked by ruff (`ruff check` and `ruff format --check` cover
`examples/`). mypy's configured `files` are `src` and `tests`, so `examples/`
is outside its scope.
