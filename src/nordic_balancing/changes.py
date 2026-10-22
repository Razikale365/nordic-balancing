"""Dated changes to how balancing data is defined.

The same series can mean something different before and after a market
design change. Check :func:`changes_between` before comparing or training on
data that spans one of these dates.

Each entry cites an announcement or the source where the change was observed.
``effective`` is the first instant (UTC) the new rule applies; for changes
published as a Danish delivery day, that is midnight Danish time on that day.

``CHANGES`` covers changes to imbalance-price definitions; changes to a
capacity market's definition are documented per dataset in
``docs/API_CONTRACT.md``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from nordic_balancing.models import BiddingZone, to_utc

_EDS_IMBALANCE = "https://www.energidataservice.dk/tso-electricity/ImbalancePrice"
_ESETT = "https://api.opendata.esett.com"
_FINGRID_IMBALANCE_2026 = (
    "https://www.fingrid.fi/en/news/news/2026/"
    "change-in-determining-the-imbalance-price-for-balance-responsible-parties-in-june-2026/"
)
_FINGRID_319 = "https://data.fingrid.fi/en/datasets/319"
_NORDIC = frozenset(BiddingZone)
_DK = frozenset({BiddingZone.DK1, BiddingZone.DK2})


@dataclass(frozen=True, slots=True)
class MarketChange:
    """One change to the definition of a balancing data series."""

    effective: datetime
    zones: frozenset[BiddingZone]
    summary: str
    source_url: str


CHANGES: tuple[MarketChange, ...] = (
    MarketChange(
        effective=datetime(2023, 5, 21, 22, 0, tzinfo=UTC),
        zones=_NORDIC,
        summary=(
            "Imbalance settlement period changes from 60 to 15 minutes, "
            "observed in eSett open data."
        ),
        source_url=_ESETT,
    ),
    MarketChange(
        effective=datetime(2025, 3, 14, 23, 15, tzinfo=UTC),
        zones=frozenset({BiddingZone.FI}),
        summary=(
            "Fingrid open data switched from hourly to 15-minute publication for "
            "datasets 319, 244, 106 and 369; hourly records overlapped the new "
            "15-minute records until 2025-03-18T23:00Z, sometimes with different "
            "values (2025-03-16T09:00Z: hourly 104, quarters 107; eSett settled 104). "
            "FingridClient uses the hourly record before 2025-03-18T23:00Z."
        ),
        source_url=_FINGRID_319,
    ),
    MarketChange(
        effective=datetime(2025, 3, 18, 23, 0, tzinfo=UTC),
        zones=_NORDIC,
        summary=(
            "15-minute imbalance prices replace the hourly price repeated per quarter, "
            "observed in eSett open data."
        ),
        source_url=_ESETT,
    ),
    MarketChange(
        effective=datetime(2025, 3, 18, 23, 0, tzinfo=UTC),
        zones=_DK,
        summary=(
            "New Danish imbalance price design: before this, the imbalance price "
            "equalled the Pricing Module imbalance price."
        ),
        source_url=_EDS_IMBALANCE,
    ),
    MarketChange(
        effective=datetime(2026, 3, 3, 23, 0, tzinfo=UTC),
        zones=_DK,
        summary=(
            "Dominating direction is determined by satisfied demand; before this "
            "it was determined by balancing demand."
        ),
        source_url=_EDS_IMBALANCE,
    ),
    MarketChange(
        # Delivery day 2026-06-01 in Finnish time; the first quarter breaking the
        # old max/min rule in Fingrid's published prices is 2026-05-31T21:00Z.
        effective=datetime(2026, 5, 31, 21, 0, tzinfo=UTC),
        zones=frozenset({BiddingZone.FI}),
        summary=(
            "The imbalance price is volume-weighted between the mFRR marginal price "
            "and the aFRR volume-weighted price when both are activated in the "
            "dominating direction; before this it was the larger (up) or smaller "
            "(down) of the two. The dominating direction is still set by mFRR."
        ),
        source_url=_FINGRID_IMBALANCE_2026,
    ),
)


def changes_between(
    start: datetime,
    end: datetime,
    zones: Iterable[BiddingZone] | None = None,
) -> list[MarketChange]:
    """Changes taking effect in ``(start, end]`` for any of ``zones`` (default: all).

    A change exactly at ``start`` is excluded: data from ``start`` onwards is
    already entirely under the new rule. ``zones`` must be a non-empty iterable
    of ``BiddingZone`` values, as in the client query methods.
    """
    start_utc = to_utc(start, "start")
    end_utc = to_utc(end, "end")
    if end_utc < start_utc:
        raise ValueError("end must be after start")
    wanted = None
    if zones is not None:
        # Validated before hashing: an unhashable member is still a bad value.
        try:
            members = list(zones)
        except TypeError as exc:
            raise ValueError("zones must contain only BiddingZone values") from exc
        if any(not isinstance(z, BiddingZone) for z in members):
            raise ValueError("zones must contain only BiddingZone values")
        if not members:
            raise ValueError("zones must not be empty")
        wanted = frozenset(members)
    return [
        change
        for change in CHANGES
        if start_utc < change.effective <= end_utc and (wanted is None or change.zones & wanted)
    ]
