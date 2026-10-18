# Benchmark harness (performance investigation)

Offline, fixture-driven benchmarking for `nordic-balancing`. Live provider
responses are recorded once into `C:\Docker\nb-perf-fixtures` (outside the
repo, never committed or redistributed — they are API payload bytes) and
replayed through `httpx` transports passed to each client's public `http=`
parameter. See `../.jpcp/perf/FIXTURES.md` for the shared fixture contract.

## Files

- `fixtures.py` — `RecordTransport` / `ReplayTransport` implementing the
  FIXTURES.md manifest format; `replay_client(set_dir)` returns an
  `httpx.Client` for a recorded set. Replay misses raise `ReplayMissError`.
- `workloads.py` — the single source of truth for the recorded client calls;
  record, run and compare all use it.
- `record.py` — records one named set per invocation on the v0.3.0 code (see
  the module docstring for the exact `uv run --project` command). Requests
  are serial and paced; API keys come from the environment and are scanned
  for afterwards. `--finalize` writes `C:\Docker\nb-perf-fixtures\READY`.
- `run.py` — the measurement harness. Each measurement is a fresh
  subprocess; a warmup child then N measured children per set (N=7 when the
  probe is sub-second, else 3; override with `--repeats`). Per child it
  reports wall, CPU user+sys, peak RSS (psutil), per-stage times measured on
  the real code path (transport, ZIP read, decode, parse+validate, record
  construction incl. the `raw` deep copy, merge `replace`), `to_frame`, a
  residual (dedupe/merge/expansion/sort), records/s and output cardinality.
  Live provider latency is reported only from the recorded `elapsed_s`.
  Output: `benchmarks/results/<tag>__<set>.json`.
- `compare_outputs.py` — dumps canonical JSONL of every record (fields +
  `raw`, in order) per set per version, and diffs two dumps. Run before and
  after any optimization commit.
- `memory_contamination.py` — measures how much of
  `examples/benchmarks/memory.py`'s timed wall is synthetic payload
  generation inside the mock handler.
- `Dockerfile` — pinned container build (Python 3.12, deps from `uv.lock`).

## Running

Locally (the `benchmarks` dependency group provides psutil and pandas):

```
uv run --group benchmarks python benchmarks/run.py --sets <set>[,<set>...] --tag <label>
uv run --group benchmarks python benchmarks/compare_outputs.py dump --tag <label>
uv run --group benchmarks python benchmarks/compare_outputs.py diff <labelA> <labelB>
```

In Docker (repeatability, not hardware independence — the container still
shares the host kernel scheduler and is affected by host load):

```
docker build -f benchmarks/Dockerfile -t nb-perf .
docker run --rm --cpus=2 --memory=4g ^
  -v C:\Docker\nb-perf-fixtures:/fixtures:ro ^
  -v %cd%\benchmarks\results:/work/benchmarks/results ^
  -e NB_FIXTURES=/fixtures -e NB_DOCKER_IMAGE=nb-perf ^
  -e NB_GIT_COMMIT=%COMMIT_SHA% ^
  nb-perf --sets entsoe-imb-SE3-year-2025 --tag docker-baseline
```

Every result JSON records the environment: host CPU/cores/RAM/OS, Python and
package versions, the git commit, the effective cgroup limits read from
`/sys/fs/cgroup`, and the image tag passed via `NB_DOCKER_IMAGE`.

## Stage semantics

Stage timers wrap the real code path during one end-to-end call and are
*inclusive* where nested: ENTSO-E `parse_validate` contains `decode`;
eSett/EDS `parse_validate` (`_parse_imbalance`) contains `construction`.
`dataclass_replace` (ENTSO-E) counts the merge `replace` calls plus cheap
`_Point` replacements. `residual_other_s` is e2e wall minus the top-level
stages: dedupe, merging, hourly expansion bookkeeping, final sorting, and
EDS capacity's inline record parsing. `to_frame` is timed outside e2e.
