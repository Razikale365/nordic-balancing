from datetime import UTC, datetime, timedelta

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

    assert len(changes_between(*window, zones=[BiddingZone.DK1])) == 3
    with pytest.raises(ValueError, match="zones must not be empty"):
        changes_between(*window, zones=[])


@pytest.mark.parametrize(
    "zones",
    [BiddingZone.DK1, "DK1", ["DK1"], ["garbage"], [42]],
)
def test_zones_must_be_a_non_empty_iterable_of_bidding_zones(zones: object) -> None:
    # A bare BiddingZone/str iterates its characters; every element check must
    # reject it, matching the client query methods.
    window = (datetime(2025, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="zones must contain only BiddingZone values"):
        changes_between(*window, zones=zones)  # type: ignore[arg-type]


def test_fi_window_returns_the_fingrid_and_nordic_changes() -> None:
    found = changes_between(
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
        zones=[BiddingZone.FI],
    )

    assert [c.effective for c in found] == [
        datetime(2025, 3, 14, 23, 15, tzinfo=UTC),
        datetime(2025, 3, 18, 23, tzinfo=UTC),
    ]
    assert found[0].zones == frozenset({BiddingZone.FI})


def test_end_before_start_raises() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        changes_between(datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))


def test_empty_window_returns_no_changes() -> None:
    assert changes_between(MARCH_2026, MARCH_2026) == []


def test_dk2_gets_the_danish_changes() -> None:
    window = (datetime(2025, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))

    found = changes_between(*window, zones=[BiddingZone.DK2])

    assert [c.effective for c in found] == [
        datetime(2025, 3, 18, 23, tzinfo=UTC),
        datetime(2025, 3, 18, 23, tzinfo=UTC),
        datetime(2026, 3, 3, 23, tzinfo=UTC),
    ]


@pytest.mark.parametrize("naive_arg", ["start", "end"])
def test_rejects_naive_datetimes(naive_arg: str) -> None:
    bounds = {
        "start": datetime(2026, 1, 1, tzinfo=UTC),
        "end": datetime(2026, 6, 1, tzinfo=UTC),
    }
    bounds[naive_arg] = bounds[naive_arg].replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        changes_between(bounds["start"], bounds["end"])


def test_rejects_non_datetime_bounds() -> None:
    with pytest.raises(TypeError, match="start must be a datetime"):
        changes_between("2026-01-01", datetime(2026, 6, 1, tzinfo=UTC))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="end must be a datetime"):
        changes_between(datetime(2026, 1, 1, tzinfo=UTC), "2026-06-01")  # type: ignore[arg-type]


def test_nordic_changes_are_observed_and_apply_to_every_zone() -> None:
    observed = [c for c in CHANGES if c.source_url == "https://api.opendata.esett.com"]
    assert len(CHANGES) == 5
    assert [c.effective for c in observed] == [
        datetime(2023, 5, 21, 22, tzinfo=UTC),
        datetime(2025, 3, 18, 23, tzinfo=UTC),
    ]
    assert all(c.zones == frozenset(BiddingZone) for c in observed)
    assert all("observed in eSett open data" in c.summary for c in observed)
    assert (
        len(
            changes_between(
                datetime(2023, 1, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
                [BiddingZone.SE3],
            )
        )
        == 2
    )


def test_unverified_fi_formula_change_has_no_entry() -> None:
    # API_CONTRACT: a possible FI formula change around 2026-06-01 is unverified,
    # so CHANGES must not record one until a primary source is found.
    window = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC))
    assert not [
        c for c in CHANGES if BiddingZone.FI in c.zones and window[0] <= c.effective < window[1]
    ]


@pytest.mark.parametrize("zones", [[[]], [{}], [BiddingZone.FI, []]])
def test_unhashable_zone_member_is_a_value_error(zones: list[object]) -> None:
    with pytest.raises(ValueError, match="BiddingZone"):
        changes_between(MARCH_2026, MARCH_2026, zones=zones)  # type: ignore[arg-type]


def test_zone_filter_keeps_a_change_that_applies_to_only_one_of_the_zones() -> None:
    fi_only = next(c for c in CHANGES if c.zones == {BiddingZone.FI})
    found = changes_between(
        fi_only.effective - timedelta(hours=1),
        fi_only.effective + timedelta(hours=1),
        zones=[BiddingZone.DK1, BiddingZone.FI],
    )
    assert fi_only in found
    assert [c.zones for c in found if c is not fi_only] == []
