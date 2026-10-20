# Benchmarks

How fast `nordic-balancing` is, how it compares with other Python clients for the same data, and
the harness that measures it.

**Method in two sentences.** Real provider responses were recorded once and replayed byte for byte
through each client's public `http=` parameter, so network latency is excluded and every client
parses identical input. Each measurement runs in a fresh subprocess after one warmup; medians of
3 runs (year windows) or 7 (shorter), on a Windows 11 workstation (i5-9400F, 6 cores, 32 GiB,
Python 3.12) that was not isolated, so expect run-to-run variation of tens of percent.

## Compared with other clients (2026-10-17, v0.3.0)

Same recorded bytes for both sides; wall time in seconds, peak resident memory in MiB.

| Workload | nordic-balancing | Other client | Prices identical |
|---|---:|---:|---|
| ENTSO-E imbalance, SE3, October 2025 | 0.73 s / 43 MiB | entsoe-py 0.8.1: 2.79 s / 113 MiB | yes, 2,976 of 2,976 |
| ENTSO-E imbalance, SE3, 2025 | 8.2 s / 140 MiB | entsoe-py: 25.9 s / 384 MiB | yes, on every common interval |
| ENTSO-E imbalance, 12 zones, 2025 | 87 s / 641 MiB | entsoe-py: 238 s / 472 MiB | yes, on every common interval |
| eSett imbalance, SE3, 2025 | 1.04 s / 89 MiB | esett-py 0.1.0: 0.12 s / 71 MiB | yes, 35,040 of 35,040 |
| eSett imbalance, 12 zones, 2025 | 17.2 s / 700 MiB | esett-py: 1.7 s / 526 MiB | yes, 420,480 of 420,480 |

- **ENTSO-E:** faster than entsoe-py on all six recorded sets (1.4 to 3.8 times); uses more
  memory on the all-zone year.
- **eSett:** esett-py is about 10 times faster. It returns the raw JSON dictionaries; this
  library parses timestamps, validates every record against the [data contracts](../README.md#data-contracts)
  and builds typed records. After the optimisation below the gap is roughly 6 to 7 times (separate
  runs, so approximate), and what is left is that work, not copying overhead.
- **Row counts differ on yearly ENTSO-E windows** because this library expands hourly-published
  values to four quarters (SE3 2025: 35,020 rows against 29,563). An independent XML decoder
  (`source_audit.py`) confirmed that neither client dropped or changed a published value.
- No other maintained client covers the Energi Data Service, Fingrid or Svenska kraftnät
  balancing workloads here, so those are measured only on their own.

## Optimisation (2026-10-18)

Profiling showed the deep copy of each record's `raw` source data was the largest cost on every
provider, almost all of it per-value dispatch rather than copying. Copying builtin JSON types
directly cut end-to-end time on year workloads with output byte-identical on all 12 recorded sets
(1.42 M records, `raw` included); peak memory is unchanged.

| Workload (2025) | v0.3.0 | after | change |
|---|---:|---:|---:|
| eSett SE3 | 1.22 s | 0.79 s | -36 % |
| eSett, 12 zones | 15.8 s | 10.9 s | -31 % |
| ENTSO-E SE3 | 4.30 s | 3.40 s | -21 % |
| ENTSO-E, 12 zones | 60.0 s | 48.4 s | -19 % |
| Energi Data Service DK | 1.75 s | 1.38 s | -21 % |

Not done yet: the duplicate-merge chain in the ENTSO-E parser builds some records three times;
it is the next-largest cost.

## Harness

The recorded responses are API payloads and are not redistributed. Scripts read them from
`$NB_FIXTURES`, or from the git-ignored `benchmarks/.fixtures/` when it is unset.
Result JSON files are written to `benchmarks/results/` and are not committed.

- `fixtures.py`: record and replay `httpx` transports; a request that was not recorded raises.
- `workloads.py`: the client calls that are recorded and measured.
- `record.py`: records one named set of live responses (API keys come from the environment).
- `run.py`: wall, CPU, peak memory and per-stage timings (transport, decode, parse, record
  construction, merge) per set.
- `compare_outputs.py`: dumps every record as canonical JSONL and diffs two versions; run before
  and after any optimisation.
- `compare.py`, `source_audit.py`: the comparison with other clients and the independent XML check.
- `Dockerfile`: pinned container build for repeatable runs.
- `arcticgrid_reference/`: a downstream consumer replaying a recorded dataset
  ([README](arcticgrid_reference/README.md)).

```bash
uv run --group benchmarks python benchmarks/run.py --sets <set>[,<set>...] --tag <label>
uv run --group benchmarks python benchmarks/compare_outputs.py diff <labelA> <labelB>
uv run --locked --group benchmarks-compare python -m benchmarks.compare
```

Memory for a full year of all 12 zones, measured with synthetic responses, is in
[examples/benchmarks/](../examples/benchmarks/README.md).
