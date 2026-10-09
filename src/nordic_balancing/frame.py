"""Convert normalised records to a pandas DataFrame (optional ``pandas`` extra).

pandas is imported only when :func:`to_frame` is called, so the rest of the
library works without it. Install it with ``pip install "nordic-balancing[pandas]"``.
"""

from collections.abc import Iterable
from dataclasses import fields
from typing import TYPE_CHECKING, Any

from nordic_balancing.models import ImbalancePrice, ReserveCapacity

if TYPE_CHECKING:
    import pandas as pd

_ENUMS = frozenset({"zone", "product"})
_DIRECTIONS = frozenset({"dominating_direction", "direction"})


def to_frame(records: Iterable[ImbalancePrice] | Iterable[ReserveCapacity]) -> "pd.DataFrame":
    """Return one row per record, in input order, with one column per field except ``raw``.

    Column types:

    * ``start``: ``datetime64`` in UTC.
    * ``zone`` and ``product``: the enum value as a string (``"FI"``, ``"mFRR"``).
    * ``dominating_direction`` and ``direction``: nullable ``Int8`` (-1, 0, 1 or ``<NA>``).
    * ``resolution``: ``timedelta64``, the source's publication interval.
    * Prices and volumes: ``float64``. ``None`` (not published, or not provided by
      the source) becomes ``NaN``.

    The index is a plain ``RangeIndex``; use ``.set_index(["start", "zone"])`` to key it.
    No records give an empty DataFrame without columns. Records must all be
    :class:`ImbalancePrice` or all :class:`ReserveCapacity`; ``raw`` is left out because
    it differs per source.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError(
            'to_frame requires pandas: pip install "nordic-balancing[pandas]"'
        ) from exc
    rows: list[Any] = list(records)
    if not rows:
        return pd.DataFrame()
    kind = type(rows[0])
    if kind not in (ImbalancePrice, ReserveCapacity) or any(type(row) is not kind for row in rows):
        raise ValueError("records must be all ImbalancePrice or all ReserveCapacity")
    columns: dict[str, Any] = {}
    for field in fields(kind):
        if field.name == "raw":
            continue
        values = [getattr(row, field.name) for row in rows]
        if field.name == "start":
            columns[field.name] = pd.to_datetime(values, utc=True)
        elif field.name == "resolution":
            columns[field.name] = pd.to_timedelta(values)
        elif field.name in _ENUMS:
            columns[field.name] = [value.value for value in values]
        elif field.name in _DIRECTIONS:
            columns[field.name] = pd.array(
                [None if value is None else int(value) for value in values], dtype="Int8"
            )
        elif field.name == "source":
            columns[field.name] = values
        else:
            columns[field.name] = pd.Series(values, dtype="float64")
    return pd.DataFrame(columns)
