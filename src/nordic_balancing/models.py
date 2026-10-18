"""Normalised data types shared by every source.

Every interval is identified by its start instant as a timezone-aware UTC
``datetime`` and lasts :data:`INTERVAL` (15 minutes) — the Nordic imbalance
settlement period.
"""

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
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

    On ``ReserveCapacity`` this is the regulation direction of the procured
    capacity; ``NONE`` marks a symmetric product whose volume is available
    both up and down; count it in both directions when summing volume per
    direction, and once when summing cost.
    """

    DOWN = -1
    NONE = 0
    UP = 1


class ReserveProduct(StrEnum):
    """Reserve capacity market products."""

    MFRR = "mFRR"
    AFRR = "aFRR"
    FCR_N = "FCR-N"
    FCR_D = "FCR-D"
    FCR = "FCR"


# Exact types only: a subclass instance can carry mutable attributes that deepcopy would clone.
_ATOMIC = frozenset({str, bytes, int, float, bool, type(None)})


def _copy_raw(value: Any) -> Any:
    """Deep-copy a JSON-like value so no nested container is shared."""
    # Exact-type checks first: provider JSON is almost all builtin dicts of scalars, and
    # the abc check behind isinstance(value, Mapping) is the expensive one.
    kind = type(value)
    if kind in _ATOMIC:
        return value  # immutable scalars deep-copy to themselves
    if kind is dict:
        return {
            key: item if type(item) in _ATOMIC else _copy_raw(item) for key, item in value.items()
        }
    if isinstance(value, Mapping):
        return {key: _copy_raw(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_raw(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_raw(item) for item in value)
    return deepcopy(value)


class _ReadOnlyMapping(Mapping[str, Any]):
    """Read-only mapping over a per-record dict.

    Unlike ``MappingProxyType`` this pickles, deep-copies and survives
    ``dataclasses.asdict``, so records holding it work with multiprocessing,
    caches and ``copy``; top-level writes still raise ``TypeError``.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(self._data)


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
    ``raw`` is a read-only mapping over a deep copy made for this record alone:
    nested containers are never shared with the source or with other records.
    Only the top level rejects writes; treat nested values as read-only too.
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
        if self.start.tzinfo is not UTC:
            # Same instant, but interval arithmetic must not run in a local wall clock.
            object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "raw", _ReadOnlyMapping(_copy_raw(self.raw)))

    @property
    def end(self) -> datetime:
        """The exclusive end of the interval."""
        return self.start + INTERVAL


@dataclass(frozen=True, slots=True)
class ReserveCapacity:
    """The reserve capacity price and procured volume for one zone and interval.

    Prices are in EUR per MW of capacity per hour; ``volume_mw`` is the procured
    MW. ``None`` means not published yet OR not provided by this source.
    ``resolution`` is the interval at which the source published the value; an
    hourly value can be repeated across four normalised intervals. ``raw``
    preserves the source record for inspecting fields that are not normalised.
    ``raw`` is a read-only mapping over a deep copy made for this record alone:
    nested containers are never shared with the source or with other records.
    Only the top level rejects writes; treat nested values as read-only too.
    """

    start: datetime
    zone: BiddingZone
    product: ReserveProduct
    direction: Direction
    price_eur_per_mw: float | None
    volume_mw: float | None
    source: str
    resolution: timedelta
    raw: Mapping[str, Any] = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.start.utcoffset() != timedelta(0):
            raise ValueError(f"start must be a UTC datetime, got {self.start.isoformat()}")
        if self.start.tzinfo is not UTC:
            # Same instant, but interval arithmetic must not run in a local wall clock.
            object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "raw", _ReadOnlyMapping(_copy_raw(self.raw)))

    @property
    def end(self) -> datetime:
        """The exclusive end of the interval."""
        return self.start + INTERVAL
