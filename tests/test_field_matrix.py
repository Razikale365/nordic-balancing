from collections.abc import Callable
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    EnergiDataServiceClient,
    EntsoeClient,
    ESettClient,
    FingridClient,
    ImbalancePrice,
    ReserveCapacity,
    ReserveProduct,
    SvKClient,
)

ROOT = Path(__file__).resolve().parents[1]
FIELD_MATRIX_DOC = ROOT / "docs" / "FIELD_MATRIX.md"
ENTSOE_FIXTURES = Path(__file__).parent / "fixtures" / "entsoe"

IMBALANCE_PRICE_FIELDS = tuple(f.name for f in fields(ImbalancePrice))
RESERVE_CAPACITY_FIELDS = tuple(f.name for f in fields(ReserveCapacity))

IMBALANCE_PRICE_AVAILABILITY: dict[str, dict[str, str]] = {
    "energidataservice": {
        "start": "provided",
        "zone": "provided",
        "imbalance_price_eur": "provided",
        "imbalance_price_dkk": "provided",
        "spot_price_eur": "provided",
        "dominating_direction": "provided",
        "satisfied_demand_mw": "provided",
        "afrr_up_vwa_eur": "provided",
        "afrr_down_vwa_eur": "provided",
        "mfrr_up_price_eur": "provided",
        "mfrr_down_price_eur": "provided",
        "source": "provided",
        "resolution": "provided",
        "raw": "provided",
    },
    "esett": {
        "start": "provided",
        "zone": "provided",
        "imbalance_price_eur": "provided",
        "imbalance_price_dkk": "—",
        "spot_price_eur": "—",
        "dominating_direction": "provided",
        "satisfied_demand_mw": "—",
        "afrr_up_vwa_eur": "—",
        "afrr_down_vwa_eur": "—",
        "mfrr_up_price_eur": "provided",
        "mfrr_down_price_eur": "provided",
        "source": "provided",
        "resolution": "provided",
        "raw": "provided",
    },
    "fingrid": {
        "start": "provided",
        "zone": "provided",
        "imbalance_price_eur": "provided",
        "imbalance_price_dkk": "—",
        "spot_price_eur": "—",
        "dominating_direction": "provided",
        "satisfied_demand_mw": "—",
        "afrr_up_vwa_eur": "—",
        "afrr_down_vwa_eur": "—",
        "mfrr_up_price_eur": "provided",
        "mfrr_down_price_eur": "provided",
        "source": "provided",
        "resolution": "provided",
        "raw": "provided",
    },
    "entsoe": {
        "start": "provided",
        "zone": "provided",
        "imbalance_price_eur": "provided",
        "imbalance_price_dkk": "—",
        "spot_price_eur": "—",
        "dominating_direction": "—",
        "satisfied_demand_mw": "—",
        "afrr_up_vwa_eur": "—",
        "afrr_down_vwa_eur": "—",
        "mfrr_up_price_eur": "—",
        "mfrr_down_price_eur": "—",
        "source": "provided",
        "resolution": "provided",
        "raw": "provided",
    },
}

RESERVE_CAPACITY_AVAILABILITY: dict[str, dict[str, str]] = {
    "svk": {
        "start": "provided",
        "zone": "provided",
        "product": "provided",
        "direction": "provided",
        "price_eur_per_mw": "provided",
        "volume_mw": "provided",
        "source": "provided",
        "resolution": "provided",
        "raw": "provided",
    },
    "energidataservice": {
        "start": "provided",
        "zone": "provided",
        "product": "provided",
        "direction": "provided",
        "price_eur_per_mw": "provided",
        "volume_mw": "provided",
        "source": "provided",
        "resolution": "provided",
        "raw": "provided",
    },
    "entsoe": {
        "start": "provided",
        "zone": "provided",
        "product": "provided",
        "direction": "provided",
        "price_eur_per_mw": "provided",
        "volume_mw": "provided",
        "source": "provided",
        "resolution": "provided",
        "raw": "provided",
    },
}


def _fetch_energidataservice_populated() -> list[ImbalancePrice]:
    start = datetime(2026, 10, 9, 2, 30, tzinfo=UTC)
    published: dict[str, Any] = {
        "TimeUTC": "2026-10-09T02:30:00",
        "PriceArea": "DK2",
        "ImbalancePriceEUR": 79.27,
        "ImbalancePriceDKK": 592.46,
        "SpotPriceEUR": 79.27,
        "DominatingDirection": 0,
        "SatisfiedDemand": 0.0,
        "aFRRVWAUpEUR": 0.0,
        "aFRRVWADownEUR": -3.53,
        "mFRRMarginalPriceUpEUR": 79.27,
        "mFRRMarginalPriceDownEUR": 40.14,
    }
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"total": 1, "records": [published]})
    )
    with EnergiDataServiceClient(httpx.Client(transport=transport), sleep=lambda _: None) as client:
        return client.imbalance_prices(start, start + INTERVAL, [BiddingZone.DK2])


def _fetch_esett_populated() -> list[ImbalancePrice]:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    published: dict[str, Any] = {
        "timestamp": "2026-01-15T01:00:00",
        "timestampUTC": "2026-01-15T00:00:00Z",
        "mba": "SE3",
        "imblSalesPrice": 21.0,
        "imblPurchasePrice": 21.0,
        "upRegPrice": 76.32,
        "downRegPrice": 21.0,
        "mainDirRegPowerPerMBA": -1.0,
        "imblSpotDifferencePrice": -55.32,
        "upRegPriceFrrA": 80.0,
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=[published]))
    with ESettClient(httpx.Client(transport=transport), sleep=lambda _: None) as client:
        return client.imbalance_prices(start, start + INTERVAL, [BiddingZone.SE3])


def _fetch_fingrid_populated() -> list[ImbalancePrice]:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    values = {319: 21.0, 244: 76.32, 106: -5.0, 369: -1.0}

    def handler(request: httpx.Request) -> httpx.Response:
        dataset = int(request.url.path.split("/")[-2])
        row = {
            "datasetId": dataset,
            "startTime": "2026-01-15T00:00:00.000Z",
            "endTime": "2026-01-15T00:15:00.000Z",
            "value": values[dataset],
        }
        return httpx.Response(
            200,
            json={
                "data": [row],
                "pagination": {"total": 1, "lastPage": 1, "nextPage": None},
            },
        )

    transport = httpx.MockTransport(handler)
    with FingridClient(
        "test-fingrid-secret",
        httpx.Client(transport=transport),
        sleep=lambda _: None,
        clock=lambda: 0.0,
    ) as client:
        return client.imbalance_prices(start, start + INTERVAL)


def _fetch_entsoe_populated() -> list[ImbalancePrice]:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    xml_bytes = (ENTSOE_FIXTURES / "balancing-v4_5-a01-single-price.xml").read_bytes()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=xml_bytes))
    with EntsoeClient(
        "test-entsoe-token",
        httpx.Client(transport=transport),
        sleep=lambda _: None,
    ) as client:
        return client.imbalance_prices(start, start + timedelta(hours=1), [BiddingZone.SE3])


def _fetch_svk_populated() -> list[ReserveCapacity]:
    start = datetime(2026, 10, 8, tzinfo=UTC)
    resource_id = "00000000-0000-0000-0000-000000000001"
    record: dict[str, Any] = {
        "_id": 191887,
        "start_time_sweden": "2026-10-08T02:00:00",
        "start_time_utc": "2026-10-08T00:00:00",
        "bidding_zone": "SE3",
        "reserve_product": "mFRRCapacityMarket",
        "reserve_direction": "up",
        "price": 7.2,
        "price_unit": "EUR-MW",
        "volume": 594.0,
        "volume_unit": "MW",
        "soda_hashbyte": "de5b139233baf982f65841f1933de612",
        "soda_identity": 185433,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/package_show"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"resources": [{"id": resource_id, "datastore_active": True}]},
                },
            )
        return httpx.Response(
            200,
            json={"success": True, "result": {"total": 1, "records": [record]}},
        )

    transport = httpx.MockTransport(handler)
    with SvKClient(httpx.Client(transport=transport), sleep=lambda _: None) as client:
        return client.capacity_market(
            ReserveProduct.MFRR, start, start + timedelta(hours=1), [BiddingZone.SE3]
        )


IMBALANCE_FETCHERS: dict[str, Callable[[], list[ImbalancePrice]]] = {
    "energidataservice": _fetch_energidataservice_populated,
    "esett": _fetch_esett_populated,
    "fingrid": _fetch_fingrid_populated,
    "entsoe": _fetch_entsoe_populated,
}


def _parse_markdown_tables(markdown: str) -> list[tuple[list[str], dict[str, list[str]]]]:
    tables: list[tuple[list[str], dict[str, list[str]]]] = []
    lines = markdown.splitlines()
    idx = 0
    while idx < len(lines):
        if lines[idx].startswith("| Field |"):
            headers = [cell.strip() for cell in lines[idx].strip("|").split("|")]
            idx += 2  # skip header and separator line
            rows: dict[str, list[str]] = {}
            while idx < len(lines) and lines[idx].startswith("|"):
                cells = [cell.strip() for cell in lines[idx].strip("|").split("|")]
                key = cells[0].strip("`")
                rows[key] = [cell.strip("`") for cell in cells[1:]]
                idx += 1
            tables.append((headers[1:], rows))
        else:
            idx += 1
    return tables


def test_field_matrix_doc_matches_dataclass_fields_and_expectations() -> None:
    text = FIELD_MATRIX_DOC.read_text(encoding="utf-8")
    assert "A field a source does not provide is" in text
    assert "price_eur_per_mw \u00d7 volume_mw \u00d7 0.25 h" in text

    tables = _parse_markdown_tables(text)
    assert len(tables) == 2

    imbalance_headers, imbalance_rows = tables[0]
    imbalance_sources = [h.split("(")[1].rstrip(")").strip("`") for h in imbalance_headers]
    assert imbalance_sources == list(IMBALANCE_PRICE_AVAILABILITY)
    assert "Unpublished interval appears as" in imbalance_rows
    assert all(imbalance_rows["Unpublished interval appears as"])
    documented_imbalance_fields = [
        k for k in imbalance_rows if k != "Unpublished interval appears as"
    ]
    assert tuple(documented_imbalance_fields) == IMBALANCE_PRICE_FIELDS
    for col_idx, source in enumerate(imbalance_sources):
        for field_name in IMBALANCE_PRICE_FIELDS:
            assert (
                imbalance_rows[field_name][col_idx]
                == IMBALANCE_PRICE_AVAILABILITY[source][field_name]
            )

    reserve_headers, reserve_rows = tables[1]
    reserve_sources = [h.split("(")[1].rstrip(")").strip("`") for h in reserve_headers]
    assert reserve_sources == list(RESERVE_CAPACITY_AVAILABILITY)
    assert "Unpublished interval appears as" in reserve_rows
    assert all(reserve_rows["Unpublished interval appears as"])
    documented_reserve_fields = [k for k in reserve_rows if k != "Unpublished interval appears as"]
    assert tuple(documented_reserve_fields) == RESERVE_CAPACITY_FIELDS
    for col_idx, source in enumerate(reserve_sources):
        for field_name in RESERVE_CAPACITY_FIELDS:
            assert (
                reserve_rows[field_name][col_idx]
                == RESERVE_CAPACITY_AVAILABILITY[source][field_name]
            )


@pytest.mark.parametrize("source", tuple(IMBALANCE_PRICE_AVAILABILITY))
def test_imbalance_price_never_provided_fields_are_none_and_column_mapping(source: str) -> None:
    records = IMBALANCE_FETCHERS[source]()
    assert records
    availability = IMBALANCE_PRICE_AVAILABILITY[source]
    assert tuple(availability) == IMBALANCE_PRICE_FIELDS

    never_provided_fields = [name for name, status in availability.items() if status == "—"]
    provided_fields = [name for name, status in availability.items() if status == "provided"]
    assert len(never_provided_fields) + len(provided_fields) == len(IMBALANCE_PRICE_FIELDS)

    for record in records:
        assert record.source == source
        never_provided_values = {name: getattr(record, name) for name in never_provided_fields}
        assert all(value is None for value in never_provided_values.values()), never_provided_values

        column_mapping = {name: getattr(record, name) for name in provided_fields}
        assert all(value is not None for value in column_mapping.values()), column_mapping


def test_reserve_capacity_never_provided_fields_are_none_and_column_mapping() -> None:
    records = _fetch_svk_populated()
    assert len(records) == 4
    availability = RESERVE_CAPACITY_AVAILABILITY["svk"]
    assert tuple(availability) == RESERVE_CAPACITY_FIELDS

    never_provided_fields = [name for name, status in availability.items() if status == "—"]
    provided_fields = [name for name, status in availability.items() if status == "provided"]
    assert never_provided_fields == []
    assert len(provided_fields) == len(RESERVE_CAPACITY_FIELDS)

    for record in records:
        assert record.source == "svk"
        never_provided_values = {name: getattr(record, name) for name in never_provided_fields}
        assert all(value is None for value in never_provided_values.values()), never_provided_values

        column_mapping = {name: getattr(record, name) for name in provided_fields}
        assert all(value is not None for value in column_mapping.values()), column_mapping


@pytest.mark.parametrize("product", [ReserveProduct.MFRR, ReserveProduct.AFRR])
def test_eds_capacity_field_matrix_and_direction_mapping(product: ReserveProduct) -> None:
    start = datetime(2026, 10, 8, tzinfo=UTC)
    row = {
        "TimeUTC": "2026-10-08T00:00:00",
        "TimeDK": "2026-10-08T02:00:00",
        "PriceArea": "DK1",
        "UpDemandMW": 5.0,
        "UpProcuredMW": 7.0,
        "UpPriceEUR": 11.0,
        "UpPriceDKK": 82.0,
        "DownDemandMW": 3.0,
        "DownProcuredMW": 2.0,
        "DownPriceEUR": 9.0,
        "DownPriceDKK": 67.0,
    }
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"total": 1, "records": [row]})
    )
    with httpx.Client(transport=transport) as http, EnergiDataServiceClient(http) as eds:
        records = eds.capacity_market(product, start, start + timedelta(hours=1))
    availability = RESERVE_CAPACITY_AVAILABILITY["energidataservice"]
    assert tuple(availability) == RESERVE_CAPACITY_FIELDS
    assert all(status == "provided" for status in availability.values())
    assert len(records) == 8
    for record in records:
        assert record.source == "energidataservice"
        assert all(getattr(record, name) is not None for name in availability)
        prefix = "Up" if record.direction.value == 1 else "Down"
        assert record.price_eur_per_mw == row[f"{prefix}PriceEUR"]
        assert record.volume_mw == row[f"{prefix}ProcuredMW"]
        assert record.raw == row
