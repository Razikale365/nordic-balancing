"""Energi Data Service reserve capacity: aFRR/mFRR capacity, FCR-N/FCR-D and FCR domestic."""

import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import (
    EDS_CAPACITY_ROW,
    eds_client,
    eds_page,
    http_client,
)

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    EnergiDataServiceClient,
    ReserveCapacity,
    ReserveProduct,
    SourceError,
)
from nordic_balancing.sources import energidataservice

START = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)
END = START + timedelta(minutes=30)
CAPACITY_NUMBERS = tuple(key for key in EDS_CAPACITY_ROW if key.startswith(("Up", "Down")))
HOUR_START = START.replace(minute=0)
FCR_START = datetime(2026, 10, 5, 22, tzinfo=UTC)


ND_TOTAL: dict[str, Any] = {
    "HourUTC": "2026-10-05T22:00:00",
    "HourDK": "2026-10-06T00:00:00",
    "PriceArea": "DK2",
    "ProductName": "FCR-N",
    "AuctionType": "Total",
    "PurchasedVolumeLocal": 8.4,
    "PurchasedVolumeTotal": 246.0,
    "PriceTotalEUR": 19.300813,
}


def eds_fcr(rows: list[dict[str, Any]], start: datetime, end: datetime) -> list[Any]:
    return eds_client(lambda _: eds_page(rows)).capacity_market(
        ReserveProduct.FCR_N, start, end, zones=(BiddingZone.DK2,)
    )


FCR_FIXTURES = Path(__file__).parent / "fixtures" / "fcr"
HOUR = timedelta(hours=1)
ND = (ReserveProduct.FCR_N, ReserveProduct.FCR_D)


def nd_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads((FCR_FIXTURES / "eds-fcr-nd.json").read_text())
    return rows


def dk1_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads((FCR_FIXTURES / "eds-fcr-dk1.json").read_text())
    return rows


def product_rows(rows: list[dict[str, Any]], product: ReserveProduct) -> list[dict[str, Any]]:
    # The request filters on ProductName; a real server returns only these rows.
    names = {name for name, (p, _) in energidataservice._FCR_PRODUCTS.items() if p is product}
    return [r for r in rows if r["ProductName"] in names]


def eds_fetch(
    rows: list[dict[str, Any]],
    product: ReserveProduct = ReserveProduct.FCR_N,
    zones: tuple[BiddingZone, ...] = (BiddingZone.DK2, BiddingZone.SE3),
    *,
    start: datetime = FCR_START,
    end: datetime = FCR_START + HOUR,
) -> list[ReserveCapacity]:
    body = json.dumps({"total": len(rows), "records": rows})
    with (
        http_client(lambda _: httpx.Response(200, text=body)) as http,
        EnergiDataServiceClient(http) as client,
    ):
        return client.capacity_market(product, start, end, zones=zones)


FALL_BACK = datetime(2025, 10, 26, tzinfo=UTC)


def fall_back_rows(
    hours: tuple[tuple[str, str], ...] = (
        ("2025-10-25T23:00:00", "2025-10-26T01:00:00"),
        ("2025-10-26T01:00:00", "2025-10-26T02:00:00"),
        ("2025-10-26T01:01:00", "2025-10-26T02:01:00"),
        ("2025-10-26T02:00:00", "2025-10-26T03:00:00"),
    ),
) -> list[dict[str, Any]]:
    # FcrNdDK2 as published on 2025-10-26: no 00:00 UTC hour, the two 02:00 Danish hours
    # as HourUTC 01:00 and 01:01. Prices tell the hours apart.
    template = [r for r in nd_rows() if r["ProductName"] == "FCR-N"]
    return [
        {**r, "HourUTC": utc, "HourDK": local, "PriceTotalEUR": float(i)}
        for i, (utc, local) in enumerate(hours)
        for r in template
    ]


# --- Live checks against the real API ----------------------------------------------------------


@pytest.mark.live
def test_live_fcr_n_on_the_october_fall_back_day() -> None:
    # FcrNdDK2 publishes 2025-10-26 00:00 UTC as HourUTC 01:00 and 01:00 UTC as 01:01.
    # The expected values match ENTSO-E A81 (UTC) and SvK Mimer for SE3, checked 2026-10-17.
    start = datetime(2025, 10, 25, 23, tzinfo=UTC)
    with EnergiDataServiceClient() as eds:
        records = eds.capacity_market(
            ReserveProduct.FCR_N, start, start + timedelta(hours=4), zones=[BiddingZone.SE3]
        )

    assert [r.start for r in records] == [start + i * INTERVAL for i in range(16)]
    hourly = [(r.raw["HourUTC"], r.volume_mw, r.price_eur_per_mw) for r in records[::4]]
    assert hourly[1:3] == [
        ("2025-10-26T01:00:00", 48.5, 16.104795),
        ("2025-10-26T01:01:00", 48.0, 16.352865),
    ]


# --- Fall-back day (25-hour day, HourUTC 01:01 row) --------------------------------------------


def test_eds_nd_maps_fall_back_hours_01_00_and_01_01_to_00_00_and_01_00() -> None:
    result = eds_fetch(fall_back_rows(), start=FALL_BACK - HOUR, end=FALL_BACK + 3 * HOUR)
    for zone in (BiddingZone.DK2, BiddingZone.SE3):
        records = [r for r in result if r.zone is zone]
        assert [r.start for r in records] == [FALL_BACK - HOUR + i * INTERVAL for i in range(16)]
        assert [r.price_eur_per_mw for r in records[::4]] == [0.0, 1.0, 2.0, 3.0]
        # raw keeps the rows as published.
        assert [r.raw["HourUTC"] for r in records[::4]] == [
            "2025-10-25T23:00:00",
            "2025-10-26T01:00:00",
            "2025-10-26T01:01:00",
            "2025-10-26T02:00:00",
        ]
        assert all(len(r.raw["auctions"]) == 2 for r in records)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (FALL_BACK, FALL_BACK + INTERVAL),
        (FALL_BACK + HOUR, FALL_BACK + 2 * HOUR),
        (FALL_BACK + HOUR + 3 * INTERVAL, FALL_BACK + 3 * HOUR),
    ],
)
def test_eds_nd_fall_back_query_always_covers_both_repeated_hours(
    start: datetime, end: datetime
) -> None:
    params: list[httpx.QueryParams] = []
    rows = [r for r in fall_back_rows() if r["HourUTC"] >= "2025-10-26"]

    def handler(request: httpx.Request) -> httpx.Response:
        params.append(request.url.params)
        query_start = datetime.fromisoformat(request.url.params["start"]).replace(tzinfo=UTC)
        query_end = datetime.fromisoformat(request.url.params["end"]).replace(tzinfo=UTC)
        records = [
            r
            for r in rows
            if query_start <= datetime.fromisoformat(r["HourUTC"]).replace(tzinfo=UTC) < query_end
        ]
        return httpx.Response(200, json={"total": len(records), "records": records})

    with http_client(handler) as http:
        result = EnergiDataServiceClient(http).capacity_market(
            ReserveProduct.FCR_N, start, end, zones=(BiddingZone.DK2, BiddingZone.SE3)
        )
    assert params[0]["start"] == f"{min(start, FALL_BACK):%Y-%m-%dT%H:%M}"
    assert params[0]["end"] == f"{max(end, FALL_BACK + 2 * HOUR):%Y-%m-%dT%H:%M}"
    assert {r.start for r in result} == {
        start + i * INTERVAL for i in range((end - start) // INTERVAL)
    }
    for record in result:
        hour = record.start.replace(minute=0)
        assert record.price_eur_per_mw == float((hour - FALL_BACK) // HOUR + 1)


def test_eds_domestic_fcr_does_not_map_01_01() -> None:
    row = {**dk1_rows()[0], "HourUTC": "2025-10-26T01:01:00", "HourDK": "2025-10-26T02:01:00"}
    with pytest.raises(SourceError, match="15-minute boundary"):
        eds_fetch(
            [row],
            ReserveProduct.FCR,
            (BiddingZone.DK1,),
            start=FALL_BACK,
            end=FALL_BACK + 2 * HOUR,
        )


def test_fall_back_day_is_the_last_sunday_of_october() -> None:
    assert [energidataservice._fall_back_day(y).date().isoformat() for y in range(2022, 2028)] == [
        "2022-10-30",
        "2023-10-29",
        "2024-10-27",
        "2025-10-26",
        "2026-10-25",
        "2027-10-31",
    ]


# --- FCR query shape ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "dataset"),
    [(ReserveProduct.AFRR, "AfrrReservesNordic"), (ReserveProduct.MFRR, "mFRRCapacityMarket")],
)
def test_capacity_query_uses_dataset_defaults_columns_and_utc(
    product: ReserveProduct, dataset: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return eds_page([])

    local = timezone(timedelta(hours=2))
    result = eds_client(handler).capacity_market(
        product, START.astimezone(local), END.astimezone(local)
    )
    assert result == []
    assert seen[0].url.path == f"/dataset/{dataset}"
    params = seen[0].url.params
    assert params["start"] == "2026-10-09T02:00"
    assert params["end"] == "2026-10-09T03:00"
    assert params["timezone"] == "UTC"
    assert json.loads(params["filter"]) == {"PriceArea": ["DK1", "DK2"]}
    assert params["columns"].split(",") == list(EDS_CAPACITY_ROW)
    assert params["sort"] == "TimeUTC asc,PriceArea asc"
    assert params["offset"] == "0"
    assert params["limit"] == "10000"


def test_eds_fcr_rejects_row_at_the_query_end() -> None:
    row = {**ND_TOTAL, "HourUTC": "2026-10-05T23:00:00"}
    with pytest.raises(SourceError, match="outside requested window"):
        eds_fcr([row], FCR_START, FCR_START + timedelta(hours=1))


@pytest.mark.parametrize(
    ("domestic", "expected_sort"),
    [(True, "HourUTC asc"), (False, "HourUTC asc,PriceArea asc,ProductName asc,AuctionType asc")],
)
def test_eds_fcr_sort_param_matches_the_dataset(domestic: bool, expected_sort: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return eds_page([])

    if domestic:
        zones: tuple[BiddingZone, ...] = (BiddingZone.DK1,)
        product = ReserveProduct.FCR
    else:
        zones = (BiddingZone.DK2,)
        product = ReserveProduct.FCR_N
    eds_client(handler).capacity_market(
        product, FCR_START, FCR_START + timedelta(hours=1), zones=zones
    )
    assert seen[0].url.params["sort"] == expected_sort


def test_eds_fcr_query_columns_dataset_filter_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(energidataservice, "PAGE_SIZE", 2)
    rows = [r for r in nd_rows() if r["ProductName"] == "FCR-N" and r["PriceArea"] == "DK2"]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        offset = int(request.url.params["offset"])
        records = rows[offset : offset + 2]
        return httpx.Response(200, json={"total": len(records), "records": records})

    with http_client(handler) as http:
        result = EnergiDataServiceClient(http).capacity_market(
            ReserveProduct.FCR_N,
            FCR_START + INTERVAL + timedelta(microseconds=1),
            FCR_START + HOUR - INTERVAL,
            zones=(BiddingZone.DK2, BiddingZone.DK2),
        )
    assert len(result) == 1 and result[0].start == FCR_START + 2 * INTERVAL
    assert len(result[0].raw["auctions"]) == 2
    assert len(seen) == 2
    params = seen[0].url.params
    assert seen[0].url.path == "/dataset/FcrNdDK2"
    assert params["timezone"] == "UTC"
    assert params["start"] == "2026-10-05T22:00" and params["end"] == "2026-10-05T22:45"
    assert json.loads(params["filter"]) == {"PriceArea": ["DK2"], "ProductName": ["FCR-N"]}
    assert set(params["columns"].split(",")) == set(rows[0])


def test_eds_fcr_d_query_filters_both_directions() -> None:
    rows = [
        r for r in nd_rows() if r["ProductName"].startswith("FCR-D") and r["PriceArea"] == "SE3"
    ]
    filters: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        filters.append(json.loads(request.url.params["filter"]))
        return httpx.Response(200, json={"total": len(rows), "records": rows})

    with http_client(handler) as http:
        result = EnergiDataServiceClient(http).capacity_market(
            ReserveProduct.FCR_D, FCR_START, FCR_START + HOUR, zones=(BiddingZone.SE3,)
        )
    assert filters == [{"PriceArea": ["SE3"], "ProductName": ["FCR-D upp", "FCR-D ned"]}]
    assert {r.direction for r in result} == {Direction.UP, Direction.DOWN}


def test_eds_domestic_query_has_no_price_area_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dataset/FcrDK1"
        assert "filter" not in request.url.params
        assert set(request.url.params["columns"].split(",")) == set(dk1_rows()[0])
        return httpx.Response(200, json={"total": 1, "records": dk1_rows()})

    with http_client(handler) as http:
        result = EnergiDataServiceClient(http).capacity_market(
            ReserveProduct.FCR,
            FCR_START,
            FCR_START + HOUR,
            zones=(BiddingZone.DK1,),
        )
    assert len(result) == 4


# --- FCR paging --------------------------------------------------------------------------------


def test_capacity_pages_until_a_short_page_and_rejects_cross_page_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nordic_balancing.sources.energidataservice.PAGE_SIZE", 1)
    offsets: list[str] = []
    rows = [EDS_CAPACITY_ROW, {**EDS_CAPACITY_ROW, "PriceArea": "DK2"}]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(request.url.params["offset"])
        return eds_page(rows[offset : offset + 1])

    result = eds_client(handler).capacity_market(ReserveProduct.MFRR, HOUR_START, END)
    assert len(result) == 16
    assert offsets == ["0", "1", "2"]
    with pytest.raises(SourceError, match="duplicate record"):
        eds_client(lambda _: eds_page([EDS_CAPACITY_ROW])).capacity_market(
            ReserveProduct.MFRR, HOUR_START, END
        )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"records": []}, "invalid total"),
        ({"total": True, "records": []}, "invalid total"),
        ({"total": 0}, "no 'records' list"),
        ({"total": 2, "records": [EDS_CAPACITY_ROW]}, "does not match"),
        ({"total": 1, "records": []}, "does not match"),
        ({"total": 0, "records": [EDS_CAPACITY_ROW]}, "does not match"),
    ],
)
def test_capacity_reuses_pagination_guards(body: dict[str, Any], message: str) -> None:
    with pytest.raises(SourceError, match=message):
        eds_client(lambda _: httpx.Response(200, json=body)).capacity_market(
            ReserveProduct.AFRR, HOUR_START, END
        )


def test_capacity_rejects_a_mismatched_total_on_a_later_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nordic_balancing.sources.energidataservice.PAGE_SIZE", 1)
    responses = iter(
        [
            eds_page([EDS_CAPACITY_ROW]),
            eds_page([{**EDS_CAPACITY_ROW, "PriceArea": "DK2"}], total=5),
        ]
    )
    with pytest.raises(SourceError, match=r"total 5 does not match 1 records.*offset 1"):
        eds_client(lambda _: next(responses)).capacity_market(ReserveProduct.AFRR, HOUR_START, END)


def test_eds_nd_paging_sorts_by_a_unique_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The server orders rows by the requested sort columns only; rows that tie come back
    # in a different order on every page request, as offset paging allows.
    rows = [r for r in nd_rows() if r["ProductName"] == "FCR-N"]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sort = request.url.params["sort"]
        calls.append(sort)
        columns = [part.split()[0] for part in sort.split(",")]
        turn = len(calls) % len(rows)
        ordered = sorted(rows[turn:] + rows[:turn], key=lambda r: [r[c] for c in columns])
        offset = int(request.url.params["offset"])
        records = ordered[offset : offset + 2]
        return httpx.Response(200, json={"total": len(records), "records": records})

    with monkeypatch.context() as patch:
        patch.setattr(energidataservice, "PAGE_SIZE", 2)
        with http_client(handler) as http:
            result = EnergiDataServiceClient(http).capacity_market(
                ReserveProduct.FCR_N,
                FCR_START,
                FCR_START + HOUR,
                zones=(BiddingZone.DK2, BiddingZone.SE3),
            )
    assert calls[0] == "HourUTC asc,PriceArea asc,ProductName asc,AuctionType asc"
    assert len(calls) > 1
    assert result == eds_fetch(rows)


def test_eds_nd_stops_paging_when_a_full_page_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A server ignoring `offset` returns the same page forever; checking row-key
    # uniqueness while paging raises on page two instead of requesting and
    # buffering without bound.
    monkeypatch.setattr(energidataservice, "PAGE_SIZE", 2)
    rows = [r for r in nd_rows() if r["ProductName"] == "FCR-N"][:2]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"total": len(rows), "records": rows})

    with (
        http_client(handler) as http,
        pytest.raises(SourceError, match="duplicate record"),
    ):
        EnergiDataServiceClient(http).capacity_market(
            ReserveProduct.FCR_N,
            FCR_START,
            FCR_START + HOUR,
            zones=(BiddingZone.DK2, BiddingZone.SE3),
        )
    assert len(calls) == 2


# --- Arguments and client lifecycle ------------------------------------------------------------


@pytest.mark.parametrize(
    ("product", "start", "end", "zones", "message"),
    [
        ("aFRR", START, END, [BiddingZone.DK1], "ReserveProduct"),
        (ReserveProduct.AFRR, START.replace(tzinfo=None), END, [BiddingZone.DK1], "timezone-aware"),
        (ReserveProduct.AFRR, START, END.replace(tzinfo=None), [BiddingZone.DK1], "timezone-aware"),
        (ReserveProduct.AFRR, END, START, [BiddingZone.DK1], "end must be after start"),
        (ReserveProduct.AFRR, START, START, [BiddingZone.DK1], "end must be after start"),
        (ReserveProduct.AFRR, START, END, [], "zones must not be empty"),
        (ReserveProduct.AFRR, START, END, ["DK1"], "BiddingZone"),
        (ReserveProduct.AFRR, START, END, [None], "BiddingZone"),
        *[
            (ReserveProduct.MFRR, START, END, [zone], "only DK1 and DK2")
            for zone in BiddingZone
            if zone not in (BiddingZone.DK1, BiddingZone.DK2)
        ],
    ],
)
def test_capacity_rejects_bad_arguments_before_http(
    product: Any, start: datetime, end: datetime, zones: Any, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("bad arguments must not send an HTTP request")

    with pytest.raises(ValueError, match=message):
        eds_client(handler).capacity_market(product, start, end, zones=zones)


# --- Malformed and null rows -------------------------------------------------------------------


def test_capacity_sorts_rows_and_accepts_deduplicated_zone_generator() -> None:
    rows = [{**EDS_CAPACITY_ROW, "PriceArea": "DK2"}, EDS_CAPACITY_ROW]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return eds_page(rows)

    result = eds_client(handler).capacity_market(
        ReserveProduct.MFRR,
        HOUR_START,
        END,
        zones=(z for z in (BiddingZone.DK2, BiddingZone.DK1, BiddingZone.DK2)),
    )
    assert json.loads(seen[0].url.params["filter"]) == {"PriceArea": ["DK1", "DK2"]}
    assert [(r.start, r.zone, r.direction) for r in result] == [
        (HOUR_START + q * INTERVAL, zone, direction)
        for q in range(4)
        for zone in (BiddingZone.DK1, BiddingZone.DK2)
        for direction in (Direction.DOWN, Direction.UP)
    ]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"TimeUTC": None}, "TimeUTC is missing"),
        ({"TimeUTC": "2026-10-09"}, "unparseable TimeUTC"),
        ({"TimeUTC": "invalid"}, "unparseable TimeUTC"),
        ({"TimeUTC": "T"}, "unparseable TimeUTC"),
        ({"TimeUTC": "2026-13-99T99:99"}, "unparseable TimeUTC"),
        ({"TimeUTC": "2026-10-09T02:15:00"}, "not on the hour"),
        ({"TimeUTC": "2026-10-09T02:00:00.000001"}, "15-minute boundary"),
        ({"TimeUTC": "2026-10-09T02:00:01"}, "15-minute boundary"),
        ({"TimeUTC": "2026-10-09T02:00:00+02:00"}, "must be UTC"),
        ({"TimeUTC": "2026-10-09T02:00:00-00:00"}, "must be UTC"),
        ({"TimeUTC": "2026-10-09T01:00:00"}, "outside requested window"),
        ({"TimeUTC": "2026-10-09T03:00:00"}, "outside requested window"),
        ({"PriceArea": "SE3"}, "unrequested PriceArea"),
        ({"PriceArea": "unknown"}, "unrequested PriceArea"),
        ({"PriceArea": None}, "unrequested PriceArea"),
        ({"PriceArea": []}, "unrequested PriceArea"),
    ],
)
def test_capacity_rejects_malformed_rows(override: dict[str, Any], message: str) -> None:
    with pytest.raises(SourceError, match=message):
        eds_client(lambda _: eds_page([{**EDS_CAPACITY_ROW, **override}])).capacity_market(
            ReserveProduct.AFRR, START, END
        )


@pytest.mark.parametrize("column", list(EDS_CAPACITY_ROW))
def test_capacity_rejects_missing_columns(column: str) -> None:
    row = {key: value for key, value in EDS_CAPACITY_ROW.items() if key != column}
    with pytest.raises(SourceError, match=f"missing capacity columns.*{column}"):
        eds_client(lambda _: eds_page([row])).capacity_market(ReserveProduct.AFRR, HOUR_START, END)


@pytest.mark.parametrize("column", CAPACITY_NUMBERS)
@pytest.mark.parametrize(
    "value", [True, "1.0", [], {}, float("nan"), float("inf"), -float("inf"), 10**400]
)
def test_capacity_rejects_invalid_numbers_including_raw_columns(column: str, value: Any) -> None:
    body = json.dumps({"total": 1, "records": [{**EDS_CAPACITY_ROW, column: value}]})
    with pytest.raises(SourceError, match=f"{column} is not (a number|finite)"):
        eds_client(lambda _: httpx.Response(200, text=body)).capacity_market(
            ReserveProduct.AFRR, HOUR_START, END
        )


def test_capacity_accepts_all_null_numbers() -> None:
    row = {**EDS_CAPACITY_ROW, **dict.fromkeys(CAPACITY_NUMBERS)}
    result = eds_client(lambda _: eds_page([row])).capacity_market(
        ReserveProduct.MFRR, HOUR_START, END
    )
    assert len(result) == 8
    assert all(r.price_eur_per_mw is None and r.volume_mw is None for r in result)
    assert all(r.raw[column] is None for r in result for column in CAPACITY_NUMBERS)


@pytest.mark.parametrize("record", [None, [], "bad", 1])
def test_capacity_rejects_non_object_record(record: Any) -> None:
    body = {"total": 1, "records": [record]}
    with pytest.raises(SourceError, match="not a JSON object"):
        eds_client(lambda _: httpx.Response(200, json=body)).capacity_market(
            ReserveProduct.AFRR, HOUR_START, END
        )


def test_capacity_rejects_duplicate_hour_zone_even_when_trimmed_out() -> None:
    with pytest.raises(SourceError, match="duplicate record"):
        eds_client(lambda _: eds_page([EDS_CAPACITY_ROW, EDS_CAPACITY_ROW])).capacity_market(
            ReserveProduct.AFRR, END - timedelta(seconds=1), END
        )


@pytest.mark.parametrize("product", (*ND, ReserveProduct.FCR))
@pytest.mark.parametrize("default", [True, False])
def test_eds_fcr_rejects_default_and_uncovered_zones(
    product: ReserveProduct, default: bool
) -> None:
    with (
        http_client(lambda _: pytest.fail("validation must precede HTTP")) as http,
        EnergiDataServiceClient(http) as client,
    ):
        coverage = "DK1" if product is ReserveProduct.FCR else "DK2, SE1, SE2, SE3 and SE4"
        with pytest.raises(ValueError, match=coverage) as exc:
            if default:
                client.capacity_market(product, FCR_START, FCR_START + HOUR)
            else:
                client.capacity_market(
                    product, FCR_START, FCR_START + HOUR, zones=(BiddingZone.NO1,)
                )
        assert "pass zones=" in str(exc.value)


@pytest.mark.parametrize("field", ["PurchasedVolumeLocal", "PriceTotalEUR"])
def test_eds_nd_null_values(field: str) -> None:
    rows = product_rows([{**r, field: None} for r in nd_rows()], ReserveProduct.FCR_N)
    result = eds_fetch(rows)
    assert len(result) == 8
    assert all(
        getattr(r, "volume_mw" if field == "PurchasedVolumeLocal" else "price_eur_per_mw") is None
        for r in result
    )


@pytest.mark.parametrize("field", ["FCRdomestic_MW", "FCRdk_EUR"])
def test_eds_domestic_null_values(field: str) -> None:
    result = eds_fetch([{**dk1_rows()[0], field: None}], ReserveProduct.FCR, (BiddingZone.DK1,))
    assert len(result) == 4
    assert all(
        getattr(r, "volume_mw" if field == "FCRdomestic_MW" else "price_eur_per_mw") is None
        for r in result
    )


@pytest.mark.parametrize("product", ND)
@pytest.mark.parametrize("failure", ["missing", "duplicate", "unknown"])
def test_eds_nd_rejects_bad_groups(product: ReserveProduct, failure: str) -> None:
    rows = product_rows(nd_rows(), product)
    if failure == "missing":
        rows = [r for r in rows if not (r["PriceArea"] == "SE3" and r["AuctionType"] == "Total")]
        message = "missing Total"
    elif failure == "duplicate":
        rows.append(next(r.copy() for r in rows if r["AuctionType"] == "Total"))
        message = "duplicate"
    else:
        rows[0]["ProductName"] = "future product"
        message = "unknown ProductName"
    with pytest.raises(SourceError, match=message):
        eds_fetch(rows, product)


@pytest.mark.parametrize("identical", [True, False])
def test_eds_nd_rejects_duplicate_auction_row(identical: bool) -> None:
    rows = product_rows(nd_rows(), ReserveProduct.FCR_N)
    duplicate = next(
        r.copy() for r in rows if r["AuctionType"] != "Total" and r["ProductName"] == "FCR-N"
    )
    if not identical:
        duplicate["PurchasedVolumeLocal"] += 1
        duplicate["PurchasedVolumeTotal"] += 1
        duplicate["PriceTotalEUR"] += 1
    rows.append(duplicate)
    with pytest.raises(SourceError, match="duplicate"):
        eds_fetch(rows)


@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.FCR])
@pytest.mark.parametrize("row", [1, None, "x"])
def test_eds_fcr_rejects_non_object_row(product: ReserveProduct, row: Any) -> None:
    zones = (BiddingZone.DK1,) if product is ReserveProduct.FCR else (BiddingZone.DK2,)
    with pytest.raises(SourceError, match="not a JSON object"):
        eds_fetch([row], product, zones)


@pytest.mark.parametrize("column", energidataservice._FCR_ND_COLUMNS[5:])
@pytest.mark.parametrize("auction", ["Total", "D-1 early"])
@pytest.mark.parametrize(
    "value", [True, "1.0", [], {}, float("nan"), float("inf"), -float("inf"), 10**400]
)
def test_eds_nd_validates_every_numeric_column_on_total_and_auction_rows(
    column: str, auction: str, value: Any
) -> None:
    total = next(
        r.copy()
        for r in nd_rows()
        if r["ProductName"] == "FCR-N" and r["PriceArea"] == "DK2" and r["AuctionType"] == "Total"
    )
    rows = [total, {**total, "AuctionType": "D-1 early"}]
    next(r for r in rows if r["AuctionType"] == auction)[column] = value
    with pytest.raises(SourceError, match=f"{column} is not (a number|finite)"):
        eds_fetch(rows, zones=(BiddingZone.DK2,))


@pytest.mark.parametrize("column", energidataservice._FCR_DK1_COLUMNS[2:])
@pytest.mark.parametrize(
    "value", [True, "1.0", [], {}, float("nan"), float("inf"), -float("inf"), 10**400]
)
def test_eds_dk1_validates_every_numeric_column(column: str, value: Any) -> None:
    row = {**dk1_rows()[0], column: value}
    with pytest.raises(SourceError, match=f"{column} is not (a number|finite)"):
        eds_fetch([row], ReserveProduct.FCR, (BiddingZone.DK1,))


@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.FCR])
def test_eds_fcr_allows_null_numeric_columns(product: ReserveProduct) -> None:
    if product is ReserveProduct.FCR:
        rows = [{**dk1_rows()[0], **dict.fromkeys(energidataservice._FCR_DK1_COLUMNS[2:])}]
        zones: tuple[BiddingZone, ...] = (BiddingZone.DK1,)
    else:
        nulls = dict.fromkeys(energidataservice._FCR_ND_COLUMNS[5:])
        rows = product_rows([{**r, **nulls} for r in nd_rows() if r["PriceArea"] == "DK2"], product)
        zones = (BiddingZone.DK2,)
    result = eds_fetch(rows, product, zones)
    assert len(result) == 4
    assert all(r.price_eur_per_mw is None and r.volume_mw is None for r in result)
    if product is ReserveProduct.FCR:
        assert result[0].raw["FCRabroad_MW"] is None
    else:
        assert all(auction["PurchasedVolumeTotal"] is None for auction in result[0].raw["auctions"])


@pytest.mark.parametrize(
    ("product", "bad"),
    [
        (product, bad)
        for product in (ReserveProduct.FCR_N, ReserveProduct.FCR)
        for bad in (
            "missing",
            "time",
            "out_of_window" if product is ReserveProduct.FCR else "zone",
            "number",
            "duplicate",
        )
    ],
)
def test_eds_fcr_rejects_malformed_rows(product: ReserveProduct, bad: str) -> None:
    domestic = product is ReserveProduct.FCR
    row = (
        dk1_rows()[0]
        if domestic
        else next(
            r
            for r in nd_rows()
            if r["ProductName"] == "FCR-N"
            and r["AuctionType"] == "Total"
            and r["PriceArea"] == "DK2"
        )
    )
    zones = (BiddingZone.DK1,) if domestic else (BiddingZone.DK2,)
    if bad == "missing":
        row.pop("HourDK")
    elif bad == "time":
        row["HourUTC"] = "2026-10-05T22:15:00"
    elif bad == "out_of_window":
        row["HourUTC"] = "2026-10-05T21:00:00"
    elif bad == "zone":
        row["PriceArea"] = "NO1"
    elif bad == "number":
        row["FCRdk_EUR" if domestic else "PriceTotalEUR"] = True
    rows = [row, row.copy()] if bad == "duplicate" else [row]
    expected = {
        "missing": "missing capacity columns",
        "time": "is not on the hour",
        "out_of_window": "outside requested window",
        "zone": "unrequested PriceArea",
        "number": "is not a number",
        "duplicate": "duplicate record",
    }[bad]
    with pytest.raises(SourceError, match=expected):
        eds_fetch(rows, product, zones)


@pytest.mark.parametrize("product", ND)
def test_eds_nd_rejects_a_known_but_unrequested_product_name(
    product: ReserveProduct,
) -> None:
    # The request filters ProductName to the queried product; a row for another
    # known product violates that filter and must raise like an unrequested
    # PriceArea does.
    names = energidataservice._FCR_PRODUCTS
    stray_name = next(name for name in names if names[name][0] is not product)
    stray = next(r for r in nd_rows() if r["ProductName"] == stray_name)
    rows = [*product_rows(nd_rows(), product), stray]
    with pytest.raises(SourceError, match=f"unrequested ProductName {stray_name!r}"):
        eds_fetch(rows, product)


@pytest.mark.parametrize(
    ("hours", "message"),
    [
        # 00:00 is published, so 01:01 cannot stand in for it.
        (
            (
                ("2025-10-26T00:00:00", "2025-10-26T02:00:00"),
                ("2025-10-26T01:00:00", "2025-10-26T02:00:00"),
                ("2025-10-26T01:01:00", "2025-10-26T02:01:00"),
            ),
            "alongside 00:00",
        ),
        # A 01:00 row that is not the displaced 02:00 Danish hour.
        (
            (
                ("2025-10-26T01:00:00", "2025-10-26T01:00:00"),
                ("2025-10-26T01:01:00", "2025-10-26T02:01:00"),
            ),
            "HourDK",
        ),
        # Not the last Sunday of October.
        ((("2025-10-19T01:01:00", "2025-10-19T03:01:00"),), "15-minute boundary"),
        ((("2025-03-30T01:01:00", "2025-03-30T03:01:00"),), "15-minute boundary"),
        # Wrong HourDK, minute or hour on the right day.
        ((("2025-10-26T01:01:00", "2025-10-26T03:01:00"),), "15-minute boundary"),
        ((("2025-10-26T01:02:00", "2025-10-26T02:02:00"),), "15-minute boundary"),
        ((("2025-10-26T00:01:00", "2025-10-26T02:01:00"),), "15-minute boundary"),
        ((("2025-10-26T01:01:30", "2025-10-26T02:01:30"),), "15-minute boundary"),
        ((("2025-10-26T01:01:00Z", "2025-10-26T02:01:00"),), "15-minute boundary"),
    ],
)
def test_eds_nd_rejects_other_off_boundary_hours(
    hours: tuple[tuple[str, str], ...], message: str
) -> None:
    day = datetime.fromisoformat(hours[0][0][:10]).replace(tzinfo=UTC)
    with pytest.raises(SourceError, match=message):
        eds_fetch(fall_back_rows(hours), start=day, end=day + 3 * HOUR)


# --- Mapping and expansion into quarter hours --------------------------------------------------


@pytest.mark.parametrize("product", [ReserveProduct.MFRR, ReserveProduct.AFRR])
def test_capacity_expands_both_directions_and_preserves_mapping(product: ReserveProduct) -> None:
    # Given a row whose procurement differs from demand, including zero and negatives.
    eds = eds_client(lambda _: eds_page([EDS_CAPACITY_ROW]))
    # When requesting one whole hour.
    result = eds.capacity_market(product, HOUR_START, END, zones=[BiddingZone.DK1])
    # Then values are levels repeated on quarters, with source resolution and raw intact.
    assert len(result) == 8
    assert [(r.start, r.zone, r.direction) for r in result] == [
        (HOUR_START + q * INTERVAL, BiddingZone.DK1, direction)
        for q in range(4)
        for direction in (Direction.DOWN, Direction.UP)
    ]
    for record in result:
        assert isinstance(record, ReserveCapacity)
        assert record.product is product
        assert record.resolution == timedelta(hours=1)
        assert record.source == "energidataservice"
        assert record.start.tzinfo is UTC
        assert record.end == record.start + INTERVAL
        assert record.raw == EDS_CAPACITY_ROW
    assert (result[0].price_eur_per_mw, result[0].volume_mw) == (5.0, 0.0)
    assert (result[1].price_eur_per_mw, result[1].volume_mw) == (-2.5, 30.0)


@pytest.mark.parametrize("zone", list(BiddingZone))
def test_capacity_afrr_covers_every_nordic_zone(zone: BiddingZone) -> None:
    # Given one synthetic aFRR row for a Nordic zone.
    row = {**EDS_CAPACITY_ROW, "PriceArea": zone.value}
    # When fetching that zone through the public method.
    result = eds_client(lambda _: eds_page([row])).capacity_market(
        ReserveProduct.AFRR, HOUR_START, END, zones=[zone]
    )
    # Then all expanded records retain the zone.
    assert len(result) == 8
    assert {r.zone for r in result} == {zone}


def test_capacity_rounds_outward_and_trims_at_subsecond_precision() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return eds_page([EDS_CAPACITY_ROW, {**EDS_CAPACITY_ROW, "TimeUTC": "2026-10-09T03:00:00"}])

    result = eds_client(handler).capacity_market(
        ReserveProduct.AFRR,
        HOUR_START + INTERVAL + timedelta(microseconds=1),
        END + timedelta(microseconds=1),
        zones=[BiddingZone.DK1],
    )
    assert seen[0].url.params["start"] == "2026-10-09T02:00"
    assert seen[0].url.params["end"] == "2026-10-09T03:01"
    assert [r.start for r in result] == [HOUR_START + q * INTERVAL for q in (2, 2, 3, 3, 4, 4)]


def test_capacity_accepts_negative_numbers_without_sign_checks() -> None:
    row = {**EDS_CAPACITY_ROW, **dict.fromkeys(CAPACITY_NUMBERS, -1.25)}
    result = eds_client(lambda _: eds_page([row])).capacity_market(
        ReserveProduct.MFRR, HOUR_START, END
    )
    assert len(result) == 8
    assert all(r.price_eur_per_mw == -1.25 and r.volume_mw == -1.25 for r in result)


@pytest.mark.parametrize("suffix", ["", "Z", "+00:00"])
def test_capacity_accepts_naive_utc_and_explicit_utc_timestamps(suffix: str) -> None:
    row = {**EDS_CAPACITY_ROW, "TimeUTC": EDS_CAPACITY_ROW["TimeUTC"] + suffix}
    result = eds_client(lambda _: eds_page([row])).capacity_market(
        ReserveProduct.AFRR, HOUR_START, END
    )
    assert len(result) == 8
    assert result[0].start == HOUR_START


def test_capacity_raw_is_private_to_each_direction_and_quarter() -> None:
    row = {**EDS_CAPACITY_ROW, "metadata": {"values": [1]}}
    result = eds_client(lambda _: eds_page([row])).capacity_market(
        ReserveProduct.AFRR, HOUR_START, END
    )
    result[0].raw["metadata"]["values"].append(2)
    assert all(r.raw["metadata"]["values"] == [1] for r in result[1:])
    assert row["metadata"]["values"] == [1]


def test_eds_fcr_expands_exactly_four_quarters() -> None:
    result = eds_fcr([ND_TOTAL], FCR_START, FCR_START + timedelta(hours=1, minutes=15))
    assert [r.start for r in result] == [FCR_START + q * INTERVAL for q in range(4)]


@pytest.mark.parametrize("product", ND)
def test_eds_nd_maps_products_directions_total_and_auctions(product: ReserveProduct) -> None:
    rows = nd_rows()
    result = eds_fetch(list(reversed(product_rows(rows, product))), product)
    expected_directions = (
        {Direction.NONE}
        if product is ReserveProduct.FCR_N
        else {
            Direction.UP,
            Direction.DOWN,
        }
    )
    assert len(result) == 4 * 2 * len(expected_directions)
    assert {r.direction for r in result} == expected_directions
    assert result == sorted(result, key=lambda r: (r.start, r.zone, r.direction))
    for record in result:
        name = (
            "FCR-N"
            if product is ReserveProduct.FCR_N
            else {
                Direction.UP: "FCR-D upp",
                Direction.DOWN: "FCR-D ned",
            }[record.direction]
        )
        source = next(
            r
            for r in rows
            if r["PriceArea"] == record.zone
            and r["ProductName"] == name
            and r["AuctionType"] == "Total"
        )
        assert record.price_eur_per_mw == source["PriceTotalEUR"]
        assert record.volume_mw == source["PurchasedVolumeLocal"]
        assert record.product is product
        assert record.resolution == HOUR
        assert record.source == "energidataservice"
        assert all(getattr(record, f.name) is not None for f in fields(ReserveCapacity))
        assert {k: v for k, v in record.raw.items() if k != "auctions"} == source
        auctions = [
            r
            for r in reversed(rows)
            if r["PriceArea"] == record.zone
            and r["ProductName"] == name
            and r["AuctionType"] != "Total"
        ]
        assert record.raw["auctions"] == auctions
    assert len({(r.start, r.zone, r.direction) for r in result}) == len(result)


def test_eds_dk1_maps_domestic_not_cross_border_and_keeps_raw() -> None:
    row = dk1_rows()[0]
    result = eds_fetch([row], ReserveProduct.FCR, (BiddingZone.DK1,))
    assert len(result) == 4
    for record in result:
        assert record.zone is BiddingZone.DK1
        assert record.direction is Direction.NONE
        assert record.product is ReserveProduct.FCR
        assert (record.volume_mw, record.price_eur_per_mw) == (4.0, 2.97)
        assert record.raw == row
        assert record.raw["FCRabroad_MW"] == 25.0
        assert record.raw["FCRcross_EUR"] == 11.79
        assert record.resolution == HOUR


@pytest.mark.parametrize("product", ND)
@pytest.mark.parametrize(
    "zone", [BiddingZone.DK2, BiddingZone.SE1, BiddingZone.SE2, BiddingZone.SE3, BiddingZone.SE4]
)
def test_eds_nd_covers_each_advertised_zone(product: ReserveProduct, zone: BiddingZone) -> None:
    rows = product_rows(
        [{**r, "PriceArea": zone.value} for r in nd_rows() if r["PriceArea"] == "DK2"], product
    )
    result = eds_fetch(rows, product, (zone,))
    assert result and {r.zone for r in result} == {zone}


def test_eds_total_without_auctions_and_old_auction_name() -> None:
    row = next(
        r
        for r in nd_rows()
        if r["ProductName"] == "FCR-N" and r["PriceArea"] == "DK2" and r["AuctionType"] == "Total"
    )
    result = eds_fetch([row], zones=(BiddingZone.DK2,))
    assert len(result) == 4 and result[0].raw["auctions"] == []
    old = {**row, "AuctionType": "D-2", "PurchasedVolumeLocal": None}
    result = eds_fetch([row, old], zones=(BiddingZone.DK2,))
    assert result[0].raw["auctions"] == [old]
    result[0].raw["auctions"][0]["AuctionType"] = "changed"
    assert result[1].raw["auctions"] == [old]


@pytest.mark.parametrize("product", [ReserveProduct.FCR_N, ReserveProduct.FCR])
@pytest.mark.parametrize(
    "timestamp",
    [
        None,
        "bad",
        "2026-10-05",
        "T",
        "2026-13-99T99:99",
        "2026-10-05T22:01:00",
        "2026-10-06T00:00:00+02:00",
    ],
)
def test_eds_fcr_timestamp_errors_name_hour_utc(product: ReserveProduct, timestamp: Any) -> None:
    rows = dk1_rows() if product is ReserveProduct.FCR else nd_rows()
    rows = [{**row, "HourUTC": timestamp} for row in rows]
    zones = (
        (BiddingZone.DK1,) if product is ReserveProduct.FCR else (BiddingZone.DK2, BiddingZone.SE3)
    )
    with pytest.raises(SourceError, match="HourUTC") as exc:
        eds_fetch(rows, product, zones)
    assert "TimeUTC" not in str(exc.value)
