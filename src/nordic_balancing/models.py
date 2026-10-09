"""Normalised data types shared by every source.

Every interval is identified by its start instant as a timezone-aware UTC
``datetime`` and lasts :data:`INTERVAL` (15 minutes) — the Nordic imbalance
settlement period.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any

INTERVAL = timedelta(minutes=15)


class BiddingZone(StrEnum):
    """Nordic bidding zones (price areas)."""

    DK1 = "DK1"
    DK2 = "DK2"
    FI = "FI"
    NO1 = "NO1"
    NO2 = "NO2"
    NO3 = "NO3"
    NO4 = "NO4"
    NO5 = "NO5"
    SE1 = "SE1"
    SE2 = "SE2"
    SE3 = "SE3"
    SE4 = "SE4"


class Direction(IntEnum):
    """The system's dominating imbalance direction in an interval.

    ``UP`` means the system was short and needed upward regulation; ``DOWN``
    means it was long. Sign convention follows Energinet's
    ``DominatingDirection`` column.
    """

    DOWN = -1
    NONE = 0
    UP = 1


def to_utc(value: datetime, name: str) -> datetime:
    """Return ``value`` in UTC, refusing naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {value.isoformat()}")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ImbalancePrice:
    """The imbalance price and its components for one zone and one interval.

    Prices are in EUR/MWh unless the field name says otherwise. ``None`` means
    not published yet OR not provided by this source. ``resolution`` is the
    interval at which the source published the value; an hourly value can be
    repeated across four normalised intervals. ``raw`` preserves the source
    record for inspecting fields that are not normalised.
    """

    start: datetime
    zone: BiddingZone
    imbalance_price_eur: float | None
    imbalance_price_dkk: float | None
    spot_price_eur: float | None
    dominating_direction: Direction | None
    satisfied_demand_mw: float | None
    afrr_up_vwa_eur: float | None
    afrr_down_vwa_eur: float | None
    mfrr_up_price_eur: float | None
    mfrr_down_price_eur: float | None
    source: str
    resolution: timedelta
    raw: Mapping[str, Any] = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.start.utcoffset() != timedelta(0):
            raise ValueError(f"start must be a UTC datetime, got {self.start.isoformat()}")
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))

    @property
    def end(self) -> datetime:
        """The exclusive end of the interval."""
        return self.start + INTERVAL
