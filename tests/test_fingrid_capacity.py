"""Fingrid reserve capacity (FCR-N, FCR-D and mFRR prices and volumes)."""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fakes import (
    FI_NO_END,
    FI_NO_START,
    fi_no_datasets,
    fi_no_fingrid_fetch,
    http_client,
)

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    FingridClient,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
)
from nordic_balancing.sources import fingrid

HOUR = timedelta(hours=1)


# --- Pacing and paging -------------------------------------------------------------------------


@pytest.mark.parametrize("total", [23, 25, True, -1])
def test_fingrid_capacity_checks_pagination_totals(total: Any) -> None:
    bodies = fi_no_datasets()
    bodies[317]["pagination"]["total"] = total
    with pytest.raises(SourceError, match="total"):
        fi_no_fingrid_fetch(ReserveProduct.FCR_N, bodies)


def test_fingrid_capacity_fetches_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fingrid, "PAGE_SIZE", 12)
    bodies = fi_no_datasets()
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dataset = int(request.url.path.split("/")[-2])
        page = int(request.url.params["page"])
        calls.append((dataset, page))
        return httpx.Response(
            200,
            json={
                "data": bodies[dataset]["data"][(page - 1) * 12 : page * 12],
                "pagination": {"total": 24, "lastPage": 2},
            },
        )

    with (
        http_client(handler) as http,
        FingridClient("test-key", http, min_interval=0) as client,
    ):
        result = client.capacity_market(ReserveProduct.FCR_N, FI_NO_START, FI_NO_END)
    assert len(result) == 96
    assert calls == [(317, 1), (317, 2), (316, 1), (316, 2)]


def test_fingrid_capacity_keeps_pacing() -> None:
    bodies = fi_no_datasets()
    sleeps: list[float] = []
    with (
        http_client(
            lambda r: httpx.Response(200, json=bodies[int(r.url.path.split("/")[-2])])
        ) as http,
        FingridClient("test-key", http, clock=lambda: 0, sleep=sleeps.append) as client,
    ):
        assert len(client.capacity_market(ReserveProduct.MFRR, FI_NO_START, FI_NO_END)) == 192
    assert sleeps == [2.0, 2.0, 2.0]


# --- Arguments and client lifecycle ------------------------------------------------------------


@pytest.mark.parametrize("product", [ReserveProduct.AFRR, ReserveProduct.FCR, "FCR-N", None])
def test_fingrid_invalid_product(product: Any) -> None:
    with (
        FingridClient("test-key") as client,
        pytest.raises(ValueError, match=r"FCR_N.*FCR_D.*MFRR"),
    ):
        client.capacity_market(product, FI_NO_START, FI_NO_END)


@pytest.mark.parametrize(
    "start,end",
    [
        (FI_NO_START, FI_NO_START),
        (FI_NO_END, FI_NO_START),
        (FI_NO_START.replace(tzinfo=None), FI_NO_END),
    ],
)
def test_fingrid_invalid_window(start: datetime, end: datetime) -> None:
    with FingridClient("test-key") as client, pytest.raises(ValueError):
        client.capacity_market(ReserveProduct.MFRR, start, end)


# --- Rejected rows -----------------------------------------------------------------------------


@pytest.mark.parametrize("dataset", [317, 316])
def test_fingrid_rejects_duplicate_start(dataset: int) -> None:
    bodies = fi_no_datasets()
    bodies[dataset]["data"].append(dict(bodies[dataset]["data"][0]))
    bodies[dataset]["pagination"]["total"] += 1
    with pytest.raises(SourceError, match="duplicate"):
        fi_no_fingrid_fetch(ReserveProduct.FCR_N, bodies)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (
            {"startTime": "2026-10-07T22:15:00Z", "endTime": "2026-10-07T23:15:00Z"},
            "60-minute record is not on the hour",
        ),
        ({"endTime": "2026-10-07T22:15:00Z"}, "capacity requires hourly records"),
        ({"endTime": "2026-10-08T00:00:00Z"}, "record duration must be 15 or 60 minutes"),
        ({"startTime": "2026-10-07T22:00:01Z"}, "startTime is not on a 15-minute boundary"),
        ({"value": "NaN"}, "value is not a number"),
        ({"value": "inf"}, "value is not a number"),
        ({"value": "-inf"}, "value is not a number"),
        ({"value": True}, "value is not a number"),
        ({"datasetId": 319}, "unexpected datasetId"),
    ],
)
def test_fingrid_rejects_invalid_capacity_row(update: dict[str, Any], message: str) -> None:
    bodies = fi_no_datasets()
    bodies[317]["data"][0].update(update)
    with pytest.raises(SourceError, match=message):
        fi_no_fingrid_fetch(ReserveProduct.FCR_N, bodies)


# A well-formed hourly row strictly before query_start or exactly at query_end
# must raise in the capacity path, not be clipped away silently.
@pytest.mark.parametrize("dataset", [317, 316])
@pytest.mark.parametrize(
    "hour", [FI_NO_START - HOUR, FI_NO_END], ids=["before_query_start", "at_query_end"]
)
def test_fingrid_capacity_rejects_rows_outside_the_window(dataset: int, hour: datetime) -> None:
    bodies = fi_no_datasets()
    bodies[dataset]["data"].append(
        {
            "datasetId": dataset,
            "startTime": hour.isoformat().replace("+00:00", "Z"),
            "endTime": (hour + HOUR).isoformat().replace("+00:00", "Z"),
            "value": 5,
        }
    )
    bodies[dataset]["pagination"]["total"] += 1
    with pytest.raises(SourceError, match="outside requested window"):
        fi_no_fingrid_fetch(ReserveProduct.FCR_N, bodies)


# --- Mapping and joining price and volume ------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "direction", "price_id", "volume_id", "price", "volume"),
    [
        (ReserveProduct.FCR_N, Direction.NONE, 317, 316, 20.1, 135.0),
        (ReserveProduct.FCR_D, Direction.UP, 318, 315, 2.5, 177.3),
        (ReserveProduct.FCR_D, Direction.DOWN, 283, 281, 6.0, 249.3),
        (ReserveProduct.MFRR, Direction.UP, 329, 327, 4.0, 406.0),
        (ReserveProduct.MFRR, Direction.DOWN, 330, 328, 1.5, 700.0),
    ],
)
def test_fingrid_exact_fixture_fields(
    product: ReserveProduct,
    direction: Direction,
    price_id: int,
    volume_id: int,
    price: float,
    volume: float,
) -> None:
    bodies = fi_no_datasets()
    result = fi_no_fingrid_fetch(product, bodies)
    directions = (
        {Direction.NONE} if product is ReserveProduct.FCR_N else {Direction.UP, Direction.DOWN}
    )
    expected_keys = {
        (FI_NO_START + q * INTERVAL, BiddingZone.FI, d) for q in range(96) for d in directions
    }
    assert len(result) == len(expected_keys)
    assert {(r.start, r.zone, r.direction) for r in result} == expected_keys
    assert result == sorted(result, key=lambda r: (r.start, r.zone, r.direction))
    for quarter in range(4):
        record = next(
            r
            for r in result
            if r.start == FI_NO_START + quarter * INTERVAL and r.direction is direction
        )
        expected = ReserveCapacity(
            start=FI_NO_START + quarter * INTERVAL,
            zone=BiddingZone.FI,
            product=product,
            direction=direction,
            price_eur_per_mw=price,
            volume_mw=volume,
            source="fingrid",
            resolution=HOUR,
            raw={"price": bodies[price_id]["data"][0], "volume": bodies[volume_id]["data"][0]},
        )
        assert record == expected
        assert record.raw == expected.raw
        assert record.end == record.start + INTERVAL


@pytest.mark.parametrize("missing", [317, 316])
def test_fingrid_pairs_union_of_hours(missing: int) -> None:
    bodies = fi_no_datasets()
    bodies[missing]["data"].pop(0)
    bodies[missing]["pagination"]["total"] -= 1
    result = fi_no_fingrid_fetch(ReserveProduct.FCR_N, bodies)
    assert len(result) == 96
    for record in result[:4]:
        assert record.price_eur_per_mw == (None if missing == 317 else 20.1)
        assert record.volume_mw == (None if missing == 316 else 135.0)
        assert record.raw["price" if missing == 317 else "volume"] is None


def test_fingrid_null_values_and_empty_datasets() -> None:
    bodies = fi_no_datasets()
    bodies[317]["data"][0]["value"] = None
    result = fi_no_fingrid_fetch(ReserveProduct.FCR_N, bodies)
    assert result[0].price_eur_per_mw is None
    assert result[0].raw["price"]["value"] is None

    bodies[317]["data"][1]["value"] = None
    bodies[316]["data"][1]["value"] = None
    result = fi_no_fingrid_fetch(ReserveProduct.FCR_N, bodies)
    assert len(result) == 96
    for index, record in enumerate(result[4:8]):
        assert record.start == FI_NO_START + (index + 4) * INTERVAL
        assert record.price_eur_per_mw is None
        assert record.volume_mw is None
        assert record.raw["price"]["value"] is None
        assert record.raw["volume"]["value"] is None

    for dataset in (317, 316):
        bodies[dataset]["data"] = []
        bodies[dataset]["pagination"]["total"] = 0
    assert fi_no_fingrid_fetch(ReserveProduct.FCR_N, bodies) == []


def test_fingrid_query_floor_clip_and_timezone() -> None:
    seen: list[httpx.Request] = []
    start = FI_NO_START + INTERVAL + timedelta(microseconds=1)
    end = FI_NO_END - INTERVAL + timedelta(microseconds=1)
    result = fi_no_fingrid_fetch(
        ReserveProduct.MFRR,
        start=start.astimezone(timezone(timedelta(hours=2))),
        end=end,
        seen=seen,
    )
    assert len(seen) == 4
    assert all(
        r.url.params["startTime"] == FI_NO_START.isoformat().replace("+00:00", "Z") for r in seen
    )
    assert all(
        r.url.params["endTime"] == FI_NO_END.isoformat().replace("+00:00", "Z") for r in seen
    )
    assert len(result) == 94 * 2
    assert all(start <= r.start < end for r in result)
