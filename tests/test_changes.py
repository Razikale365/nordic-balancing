from datetime import UTC, datetime

import pytest

from nordic_balancing import CHANGES, BiddingZone, changes_between

MARCH_2026 = datetime(2026, 3, 3, 23, 0, tzinfo=UTC)


def test_changes_are_ordered_utc_and_sourced() -> None:
    assert list(CHANGES) == sorted(CHANGES, key=lambda c: c.effective)
    for change in CHANGES:
        assert change.effective.tzinfo is UTC
        assert change.zones
        assert change.source_url.startswith("https://")


def test_window_spanning_a_change_reports_it() -> None:
    found = changes_between(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC))

    assert [c.effective for c in found] == [MARCH_2026]


def test_window_starting_at_a_change_does_not_report_it() -> None:
    assert changes_between(MARCH_2026, datetime(2026, 6, 1, tzinfo=UTC)) == []


def test_window_ending_at_a_change_reports_it() -> None:
    found = changes_between(datetime(2026, 1, 1, tzinfo=UTC), MARCH_2026)

    assert [c.effective for c in found] == [MARCH_2026]


def test_filters_by_zone() -> None:
    window = (datetime(2025, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))

    assert len(changes_between(*window, zones=[BiddingZone.DK1])) == 2
    assert changes_between(*window, zones=[]) == []


def test_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        changes_between(datetime(2026, 1, 1), datetime(2026, 6, 1, tzinfo=UTC))
