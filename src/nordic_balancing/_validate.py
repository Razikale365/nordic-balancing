"""Shared record validators enforcing the data contracts in README.md."""

import math
from collections.abc import Hashable

from nordic_balancing.errors import SourceError


def finite_number(value: object, *, source: str, column: str) -> float | None:
    """Return ``value`` as a float, ``None`` for null; reject the rest (C5).

    A JSON bool, a non-number, a NaN/Infinity token and an overflowing value
    are all rejected. The offending value is never echoed: Fingrid fields can
    contain the API key.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SourceError(f"{source}: {column} is not a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise SourceError(f"{source}: {column} is not finite") from exc
    if not math.isfinite(number):
        raise SourceError(f"{source}: {column} is not finite")
    return number


def reject_duplicate[K: Hashable](seen: set[K], key: K, *, source: str, detail: str) -> None:
    """Reject a second record for the same series key (C3)."""
    if key in seen:
        raise SourceError(f"{source}: duplicate {detail}")
    seen.add(key)
