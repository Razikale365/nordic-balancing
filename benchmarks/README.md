# Benchmarks

How fast `nordic-balancing` is, how it compares with other Python clients for the same data, and
the harness that measures it.

**Method in two sentences.** Real provider responses were recorded once and replayed byte for byte
through each client's public `http=` parameter, so network latency is excluded and every client
parses identical input. Each measurement runs in a fresh subprocess after one warmup; medians of
3 runs (year windows) or 7 (shorter), on a Windows 11 workstation (i5-9400F, 6 cores, 32 GiB,
Python 3.12) that was not isolated, so expect run-to-run variation of tens of percent.

## Compared with other clients

Same recorded bytes for both sides; wall time in seconds, peak resident memory in MiB. Both
providers were measured after the parsing work below (2026-10-20).

| Workload | nordic-balancing | Other client | Prices identical |
|---|---:|---:|---|
| ENTSO-E imbalance, SE3, October 2025 | 0.18 s / 45 MiB | entsoe-py 0.8.1: 1.42 s / 116 MiB | yes, 2,976 of 2,976 |
| ENTSO-E imbalance, SE3, 2025 | 2.2 s / 143 MiB | entsoe-py: 13.8 s / 388 MiB | yes, on every common interval |
| ENTSO-E imbalance, 12 zones, 2025 | 30 s / 659 MiB | entsoe-py: 163 s / 474 MiB | yes, on every common interval |
| eSett imbalance, SE3, 2025 | 0.39 s / 90 MiB | esett-py 0.1.0: 0.11 s / 72 MiB | yes, 35,040 of 35,040 |
| eSett imbalance, 12 zones, 2025 | 4.8 s / 672 MiB | esett-py: 1.4 s / 527 MiB | yes, 420,474 of 420,474 (see below) |

- **ENTSO-E:** faster than entsoe-py on all six recorded sets (4.4 to 10 times; 1.4 to 3.8 times
  on v0.3.0); uses more memory on the all-zone year.
- **eSett:** esett-py is 3.4 to 3.6 times faster (about 10 times on v0.3.0). It returns the raw
  JSON dictionaries; this library parses timestamps, validates every record against the
  [data contracts](../README.md#data-contracts) and builds a typed record with its own read-only
  `raw` per interval. JSON decoding, most of what esett-py does, is about a seventh of the
  remaining time in a profile of the SE3 year; the rest is that per-record validation and
  construction. The 6 values that differ on the all-zone year are unsettled Danish intervals
  published as all-zero placeholders, which this library reports as `None` and esett-py as `0.0`.
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

The duplicate-merge chain in the ENTSO-E parser, which built some records three times, was
removed later (see below).

## eSett parsing (2026-10-20)

A second pass on the eSett path removed per-record work the contracts do not need:

- each timestamp string is parsed once per call (all zones share it);
- a settled row's five numbers are checked together when they are all plain finite floats, with
  the column-by-column checks (and their errors, in the same order) as the fallback;
- the duplicate check formats its message only when it fails;
- quarter-hour records skip the hourly expansion loop;
- the parser builds records through an internal constructor that skips the checks it has already
  done and takes over the freshly decoded row as the record's `raw` instead of copying it again.
  Nothing else references that row; the quarters of an hourly row and every record built through
  the public constructor or `dataclasses.replace` still get their own deep copy.

Output is byte-identical on all 12 recorded sets. Before and after alternated, 7 fresh processes
each, on an idle machine:

| Workload (2025) | before | after | change |
|---|---:|---:|---:|
| eSett SE3 | 0.85 s | 0.45 s | -47 % |
| eSett, 12 zones | 11.8 s | 5.3 s | -55 % |

Peak memory is unchanged on SE3 (127 MiB) and 3 % lower on 12 zones (689 to 669 MiB).

## ENTSO-E and Energi Data Service parsing (2026-10-20)

Profiles of every recorded set showed the same pattern as on eSett: per-record work the
contracts do not need. ENTSO-E rescanned each XML element's children once per field it read
(a third of the FCR-N capacity profile), built every interval published in two price categories
three times through `dataclasses.replace`, and copied each filled point the same way. Energi Data
Service deep-copied each capacity row once per emitted record (eight times for aFRR and mFRR)
and re-parsed the same timestamps and auction keys on every row.

- each XML element's children are indexed by name in one pass;
- an ENTSO-E interval is built once; a second category's publication is checked with the same
  rules, in the same order, and merged into that record's own `raw` before anything returns it;
- filled points are built directly instead of through `dataclasses.replace`;
- parsers build records through the internal constructor, which takes over a freshly built `raw`;
  where one source row feeds several records (hourly quarters, both directions), one record takes
  it over and every other one gets its own deep copy;
- Energi Data Service caches repeated timestamps and auction keys within a call and checks the
  October fall-back pattern only on rows that can match it.

Output is byte-identical on all 12 recorded sets, and a differential run of 15,000 generated
documents (duplicate categories, gaps, conflicting directions, invalid numbers, fall-back-day
rows) gave identical records and errors. Before and after alternated in fresh processes on an
idle machine (7 runs below 2 s, 5 for years, 3 for all zones):

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
| eSett SE3, 2025 (code unchanged) | 0.45 s | 0.41 s | -11 % |

The eSett row is a control: its code did not change, so about 10 % is run-to-run noise on this
machine. Peak memory is unchanged within 3 %, except Energi Data Service FCR-N (388 to 360 MiB).
Fingrid and Svenska kraftnät have no recorded sets; they share the record constructors but
were not changed, and Fingrid's requests are paced at 2 s each, so its time is mostly waiting.

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
