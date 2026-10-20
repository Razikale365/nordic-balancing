# FI and NO mFRR capacity evidence

Run only when live API access is authorized. Importing this script fetches nothing.
Set `FINGRID_API_KEY` and `ENTSOE_API_KEY` in the environment.

```powershell
uv run python examples/fi_no_capacity/cross_check.py --live --start 2026-10-07T22:00Z --end 2026-10-08T22:00Z
$LASTEXITCODE
uv run python examples/fi_no_capacity/cross_check.py --live --find-coverage
$LASTEXITCODE
```

The comparison requires every requested hour/direction and four equal quarters
per hour. It prints matched counts, differing values and missing hour-directions
for ENTSO-E FI versus Fingrid and ENTSO-E SE3 versus SvK. Any missing, differing,
unexpected or unpublished value fails. SE3 uses the private validated capacity
path solely for evidence; the public API remains bounded to NO/FI.

Coverage mode searches each NO zone and FI by UTC day from an empty lower bound
(default 2010-01-01) to a recent published day, then checks both sides of the
boundary. It assumes one coverage boundary; isolated historical gaps can defeat
binary search. The output is the first UTC day with data, not necessarily the
first local delivery day or exact first hourly timestamp. The coverage starts
quoted in `docs/API_CONTRACT.md` were found by a live search.
