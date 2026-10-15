# SE3 aFRR capacity cross-source check

From the checkout root, the orchestrator may run this command **on its own, with
no other live test or source check running**:

```powershell
uv run python examples/eds_capacity/validate.py --live
$LASTEXITCODE
```

This fetches the last seven complete UTC days for SE3 aFRR, first from Energi
Data Service and then from Svenska kraftnät. To reproduce a fixed window ending
at a particular UTC hour, pass `--end 2026-10-15T00:00:00Z`. `--out path.json`
selects the report location; the default is `results.json` beside the script.
Without `--live`, the script exits with code 2 before constructing clients or
fetching anything. Importing it never fetches data.

The comparison validates four equal quarters per published hour and direction,
then compares hourly price and procured volume with exact equality (no tolerance).
`matches` counts matching hour/directions; `shared_hours` counts distinct hours
with at least one shared direction. `differences` records both sources' values.
`eds_only` lists every hour/direction only EDS publishes, including its price and
volume; `eds_only_hours` counts the distinct hours represented by those entries.
EDS publishes priced zero-volume rows that SvK can omit, so these are expected
only when the EDS procured volume is zero. `svk_only` lists the reverse difference.

Exit 0 requires at least one shared hour/direction, exact equality on every shared
value, EDS coverage of all 168 hours in both directions, no SvK-only entries and
zero procurement in every EDS-only entry. Differences and coverage errors write
a `FAIL` report and exit 1. Malformed source responses raise `SourceError` and
stop immediately. The script does not fill gaps or choose a preferred source.

Offline checks:

```powershell
uv run pytest -q tests/test_energidataservice.py -k comparison
```

The B3 implementer did not run the live comparison. No equality or coverage result
from the upstream services is claimed by this example.
