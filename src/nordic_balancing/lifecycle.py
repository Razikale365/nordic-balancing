"""Recognise records that fit a known placeholder pattern.

A placeholder is a number a source returns in place of a value it has not filled in
yet. This module looks at one record and names the pattern it matches, if any.
"""

from enum import StrEnum

from nordic_balancing.models import ImbalancePrice, ReserveCapacity
from nordic_balancing.sources import energidataservice, entsoe


class PlaceholderPattern(StrEnum):
    """A known placeholder pattern that one record can match."""

    EDS_AFRR_AVERAGE = "eds_afrr_average"
    ENTSOE_LONE_PLACEHOLDER = "entsoe_lone_placeholder"


def placeholder_pattern(record: ImbalancePrice | ReserveCapacity) -> PlaceholderPattern | None:
    """Name the placeholder pattern ``record`` matches, or return ``None``.

    ``EDS_AFRR_AVERAGE``: an Energi Data Service ``ImbalancePrice`` whose
    ``afrr_up_vwa_eur`` or ``afrr_down_vwa_eur`` is ``0.0`` while
    ``imbalance_price_eur`` is still ``None``. ``ENTSOE_LONE_PLACEHOLDER``: an
    ENTSO-E mFRR ``ReserveCapacity`` with ``raw["lone_placeholder"]`` set to
    ``True``, which the client sets on records from a Period of one zero/zero
    Point that no other series in the response replaced.

    A match means the record fits a known placeholder pattern; the value can
    still turn out to be real. An aFRR average of ``0.0`` often stays ``0.0``,
    and a zero/zero day can be genuine. Patterns that need a second source or a
    later poll, such as eSett's Finnish ``0.0``, Fingrid's zero day or a partial
    FCR total, cannot be seen in one record and are not covered; see "Value
    lifecycle" in the API contract. Raises ``TypeError`` for anything that is
    not an ``ImbalancePrice`` or ``ReserveCapacity``.
    """
    if isinstance(record, ImbalancePrice):
        if (
            record.source == energidataservice.SOURCE
            and record.imbalance_price_eur is None
            and (record.afrr_up_vwa_eur == 0.0 or record.afrr_down_vwa_eur == 0.0)
        ):
            return PlaceholderPattern.EDS_AFRR_AVERAGE
        return None
    if isinstance(record, ReserveCapacity):
        if record.source == entsoe.SOURCE and record.raw.get("lone_placeholder") is True:
            return PlaceholderPattern.ENTSOE_LONE_PLACEHOLDER
        return None
    raise TypeError("record must be an ImbalancePrice or ReserveCapacity")
