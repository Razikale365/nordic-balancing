"""Dated changes to how balancing data is defined.

The same series can mean something different before and after a market
design change. Check :func:`changes_between` before comparing or training on
data that spans one of these dates.

Each entry cites the source that announced it. ``effective`` is the first
instant (UTC) the new rule applies; for changes published as a Danish delivery
day, that is midnight Danish time on that day.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from nordic_balancing.models import BiddingZone, to_utc

_EDS_IMBALANCE = "https://www.energidataservice.dk/tso-electricity/ImbalancePrice"
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
)


def changes_between(
    start: datetime,
    end: datetime,
    zones: Iterable[BiddingZone] | None = None,
) -> list[MarketChange]:
    """Changes taking effect in ``(start, end]`` for any of ``zones`` (default: all).

    A change exactly at ``start`` is excluded: data from ``start`` onwards is
    already entirely under the new rule.
    """
    start_utc = to_utc(start, "start")
    end_utc = to_utc(end, "end")
    wanted = None if zones is None else frozenset(zones)
    return [
        change
        for change in CHANGES
        if start_utc < change.effective <= end_utc and (wanted is None or change.zones & wanted)
    ]
