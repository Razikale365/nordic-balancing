# Memory benchmark

`memory.py` measures peak memory and wall time for fetching one year of
imbalance prices for all 12 bidding zones. It is fully offline: every response
is synthetic and served through `httpx.MockTransport`, so no network access and
no API keys are needed.

eSett responses are JSON arrays of records shaped like the ones in
`tests/test_esett.py`. ENTSO-E responses are ZIP bodies holding one
schema-4.4 `Balancing_MarketDocument` per zone and chunk — `curveType` A03 with
two `TimeSeries` per `Period` (categories A04 and A05 at equal prices), the
shape described under "Observed documents" in `docs/RELEASE_EVIDENCE.md`. One
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

## Results (run 2026-10-14, measured under load)

Machine: Windows 11 (10.0.22621), 6 CPUs, 31.9 GiB RAM, Python 3.12.13.
Wall times were measured under load — other work was running on the machine;
rerun timing on an idle machine before trusting it. Memory figures are
unaffected by load.

| Scenario | Records | Wall s | Peak RSS MiB | Traced peak MiB | Retained MiB | Frame +peak MiB | Frame +retained MiB | B/record |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| esett-2025 | 420,480 | 122.7 | 1,344 | 401 | 330 | 141 | 39 | 3,351 |
| esett-2022 | 420,480 | 61.0 | 813 | 274 | 237 | 141 | 39 | 2,027 |
| entsoe-2025 | 420,480 | 274.0 | 2,352 | 928 | 490 | 141 | 39 | 5,865 |
| entsoe-2022 | 420,480 | 153.6 | 1,483 | 556 | 449 | 141 | 39 | 3,698 |

## Threshold check

Threshold: 1.5 GiB peak RSS or 3 KiB per record. Per scenario:

- `esett-2025`: **crossed** — bytes/record (3,351 > 3,072); peak RSS stays
  under (1,344 MiB).
- `esett-2022`: not crossed (813 MiB, 2,027 B/record).
- `entsoe-2025`: **crossed** — both measures (2,352 MiB > 1,536 MiB and
  5,865 > 3,072 B/record).
- `entsoe-2022`: **crossed** — bytes/record (3,698 > 3,072); peak RSS stays
  under (1,483 MiB).

Observations, without proposing a fix: the gap between peak RSS and traced
peak is largest for `entsoe-2025` (~1.4 GiB), which is where expat's C-level
element trees live; traced retained is roughly half the traced peak there,
the other half being transient parse objects. `to_frame()` adds about
140 MiB of traced peak and 39 MiB retained on top of the records in every
scenario, independent of source.

The script is checked by ruff (`ruff check` and `ruff format --check` cover
`examples/`). mypy's configured `files` are `src` and `tests`, so `examples/`
is outside its scope.
