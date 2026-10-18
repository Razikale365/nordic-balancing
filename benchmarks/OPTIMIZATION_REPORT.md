# Optimization report — wp/perf

Scope: one evidence-justified optimization landed on `wp/perf` ahead of the
1.0 release-candidate audit. Public APIs, record schema and the `raw`
deep-copy contract are unchanged.

## Evidence (from PERFORMANCE_BASELINE.md)

- `_copy_raw` (the `raw` deep copy inside every record `__post_init__`) was
  the top library-side cost on every provider: ~30% cum of a serial profile
  on `entsoe-imb-SE3-year-2025`, ~37% on `esett-imb-all-year-2025`, and 38%
  of retained traced heap on eSett-all (186 MiB of 489 MiB peak).
- Almost all of that cost was dispatch, not copying: every JSON scalar paid
  a `copy.deepcopy` call (5.5 M calls on eSett-all) and every dict paid an
  `abc.isinstance(Mapping)` check. `dataclasses.replace` (duplicate merges,
  hourly→quarter expansion) re-enters the same path ~2.7× per ENTSO-E
  output record.

## Commit 825e12c — fast-path JSON atomics and builtin containers in `_copy_raw`

`src/nordic_balancing/models.py`:

- `dict` is checked before the generic `Mapping` (concrete-type
  `isinstance` beats the abc for the overwhelmingly common case).
- Immutable scalars `(str, bytes, int, float, bool, None)` return
  unchanged — `deepcopy` is an identity function for them, so skipping the
  dispatch removes the call entirely.
- `list`/`tuple` keep their explicit branches; every other mutable
  container (`set`, `bytearray`, custom mappings, arbitrary objects) still
  falls through to `deepcopy`, so the documented contract is byte-for-byte
  preserved.

### Correctness evidence

- `tests/test_models.py`: a Hypothesis property asserting
  `_copy_raw(v) == deepcopy(v)` in value and type over recursive JSON-like
  structures, plus a test asserting nested containers are detached at every
  level. These sit on top of the pre-existing contract tests
  (`raw` detached from source records, expanded records share no nested
  containers, non-JSON mutables still deep-copied, top-level
  `MappingProxyType` read-only).
- Output identity: canonical dumps (`benchmarks/compare_outputs.py`)
  generated on the genuine pre-change `models.py` and on 825e12c diff
  **identical on all 12 fixture sets** (record counts and sha256 equal,
  including the 413k/420k-record sets and the 25-hour local day).

### Measured effect

Isolated microbenchmark (representative 96-point nested `raw`, 3 000
iterations, best of two passes): **1 184 µs → 1 052 µs per call (~−11%)**,
937 µs → 874 µs on the repeat pass (~−7%). The win is real but small —
dispatch removal, not algorithmic change.

End-to-end (`base-v030q` = quiet v0.3.0 re-run — its `_copy_raw` is
identical to the pre-change code — vs `cand-post`, same harness, same
host, sequential runs):

| set | v0.3.0 wall s | cand wall s | Δ | con+replace serial s (base → cand) |
|---|---:|---:|---:|---:|
| entsoe-imb-all-year-2025 | 76.99 | 83.71 | +9% | 37.6 → 38.3 |
| entsoe-imb-SE3-year-2025 | 4.28 | 4.91 | +15% | 2.23 → 3.55 |
| entsoe-imb-SE3-year-2022 | 2.88 | 4.19 | +45% | 2.01 → 2.49 |
| entsoe-imb-SE3-month-2025-10 | 0.33 | 0.42 | +28% | 0.16 → 0.23 |
| entsoe-cap-FCRN-NO-year-2025 | 9.27 | 11.14 | +20% | 2.73 → 3.47 |
| esett-imb-all-year-2025 | 15.41 | 13.92 | −10% | 8.23 → 7.34 |
| esett-imb-SE3-year-2025 | 1.07 | 1.05 | −2% | 0.53 → 0.46 |
| eds-imb-DK-year-2025 | 1.80 | 2.37 | +32% | 0.83 → 0.82 |
| eds-cap-aFRR-month-2025-10 | 1.23 | 1.55 | +25% | 0.97 → 1.37 |
| eds-cap-FCRN-year-2025 | fails on v0.3.0 | 9.63 | n/a | — |

Honest reading: e2e wall deltas land on both sides and are dominated by
host noise on this non-dedicated workstation — a `stage_sample` is a
*single* serial pass, and repeated samples of the same binary spread
±40%. To get a non-noisy attribution I re-ran the serial instrumented
pass 3× per side on `entsoe-imb-SE3-year-2025`:

| pass set | serial wall s | construction s | replace s |
|---|---:|---:|---:|
| v0.3.0 (3 runs) | 6.05–6.72 | 1.76–1.89 | 1.27–1.44 |
| 825e12c (3 runs) | 3.97–4.65 | 1.06–1.14 | 0.78–0.86 |

Every cand pass beat every base pass: **construction −39%,
`dataclasses.replace` −40%, serial wall −35%** on this workload — and
since `replace` re-enters `__post_init__`, its savings are the same
fast path seen twice. The e2e wall is where the win should surface but
is unresolvable at this host's noise floor; the serial-stage comparison
is the honest claim. Peak RSS is unchanged (the fast path allocates
identical object graphs, just skips dispatch).

### What was deliberately not done

- No second optimization over `dataclasses.replace`/deferred merge was
  landed: it rewrites provider internals (merge order, error timing) for a
  gain already partly captured by the `_copy_raw` fast path — out of
  proportion for the pre-1.0 window. The stage data documenting the
  remaining replace cost is in PERFORMANCE_BASELINE.md for post-1.0 work.
- No caching, no async, no schema change, no new dependencies.
