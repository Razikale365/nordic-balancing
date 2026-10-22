"""Shared record validators enforcing the data contracts in README.md."""

import math
from collections.abc import Hashable
from typing import TypeVar

from nordic_balancing.errors import SourceError


def finite_number(value: object, *, source: str, column: str) -> float | None:
    """Return ``value`` as a float, ``None`` for null; reject the rest (C5).

    A JSON bool, a non-number, a NaN/Infinity token and an overflowing value
    are all rejected. The offending value is never echoed: Fingrid fields can
    contain the API key.
    """
    if value is None:
        return None
    if type(value) is float and math.isfinite(value):
        return value  # the common case; float() of an exact float is the same value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SourceError(f"{source}: {column} is not a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise SourceError(f"{source}: {column} is not finite") from exc
    if not math.isfinite(number):
        raise SourceError(f"{source}: {column} is not finite")
    return number


K = TypeVar("K", bound=Hashable)


def reject_duplicate(seen: set[K], key: K, *, source: str, detail: str) -> None:
    """Reject a second record for the same series key (C3)."""
    if key in seen:
        raise SourceError(f"{source}: duplicate {detail}")
    seen.add(key)
