import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from nordic_balancing import (
    INTERVAL,
    BiddingZone,
    Direction,
    ImbalancePrice,
    ReserveCapacity,
    ReserveProduct,
    to_frame,
)

START = datetime(2025, 3, 18, 22, 45, tzinfo=UTC)
HOUR = timedelta(hours=1)


def price(
    start: datetime = START,
    zone: BiddingZone = BiddingZone.FI,
    eur: float | None = 21.5,
    direction: Direction | None = Direction.UP,
    resolution: timedelta = INTERVAL,
) -> ImbalancePrice:
    return ImbalancePrice(
        start=start,
        zone=zone,
        imbalance_price_eur=eur,
        imbalance_price_dkk=None,
        spot_price_eur=None,
        dominating_direction=direction,
        satisfied_demand_mw=None,
        afrr_up_vwa_eur=None,
        afrr_down_vwa_eur=None,
        mfrr_up_price_eur=30.0,
        mfrr_down_price_eur=None,
        source="esett",
        resolution=resolution,
        raw={"anything": [1, 2]},
    )


def capacity(direction: Direction = Direction.DOWN) -> ReserveCapacity:
    return ReserveCapacity(
        start=START,
        zone=BiddingZone.SE3,
        product=ReserveProduct.MFRR,
        direction=direction,
        price_eur_per_mw=2.47,
        volume_mw=None,
        source="svk",
        resolution=HOUR,
        raw={},
    )


def test_imbalance_prices_become_typed_columns_without_raw() -> None:
    records = [
        price(),
        price(START + INTERVAL, BiddingZone.DK1, eur=None, direction=None, resolution=HOUR),
    ]
    frame = to_frame(records)

    assert list(frame.columns) == [
        "start",
        "zone",
        "imbalance_price_eur",
        "imbalance_price_dkk",
        "spot_price_eur",
        "dominating_direction",
        "satisfied_demand_mw",
        "afrr_up_vwa_eur",
        "afrr_down_vwa_eur",
        "mfrr_up_price_eur",
        "mfrr_down_price_eur",
        "source",
        "resolution",
    ]
    assert str(frame["start"].dt.tz) == "UTC"
    assert list(frame["start"]) == [pd.Timestamp(START), pd.Timestamp(START + INTERVAL)]
    assert list(frame["zone"]) == ["FI", "DK1"]
    assert frame["imbalance_price_eur"].dtype == "float64"
    assert frame["imbalance_price_eur"].iloc[0] == 21.5
    assert pd.isna(frame["imbalance_price_eur"].iloc[1])
    assert frame["imbalance_price_dkk"].isna().all()
    assert str(frame["dominating_direction"].dtype) == "Int8"
    assert frame["dominating_direction"].iloc[0] == 1
    assert frame["dominating_direction"].iloc[1] is pd.NA
    assert list(frame["resolution"]) == [pd.Timedelta(INTERVAL), pd.Timedelta(HOUR)]
    assert list(frame["source"]) == ["esett", "esett"]


def test_keeps_input_order_and_accepts_any_iterable() -> None:
    later = price(START + INTERVAL)
    frame = to_frame(record for record in [later, price()])
    assert list(frame["start"]) == [pd.Timestamp(START + INTERVAL), pd.Timestamp(START)]
    assert isinstance(frame.index, pd.RangeIndex)


def test_reserve_capacity_columns() -> None:
    frame = to_frame([capacity(Direction.DOWN), capacity(Direction.UP)])
    assert list(frame.columns) == [
        "start",
        "zone",
        "product",
        "direction",
        "price_eur_per_mw",
        "volume_mw",
        "source",
        "resolution",
    ]
    assert list(frame["product"]) == ["mFRR", "mFRR"]
    assert list(frame["direction"]) == [-1, 1]
    assert str(frame["direction"].dtype) == "Int8"
    assert frame["volume_mw"].isna().all()


def test_no_records_give_an_empty_frame() -> None:
    frame = to_frame([])
    assert frame.empty
    assert list(frame.columns) == []


@pytest.mark.parametrize("records", [[price(), capacity()], [capacity(), price()], [object()]])
def test_rejects_mixed_or_foreign_records(records: list[Any]) -> None:
    with pytest.raises(ValueError, match="all ImbalancePrice or all ReserveCapacity"):
        to_frame(records)


def test_missing_pandas_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pandas", None)
    with pytest.raises(ImportError, match=r"nordic-balancing\[pandas\]"):
        to_frame([price()])


def test_library_imports_without_pandas() -> None:
    # A fresh interpreter in which importing pandas fails, as in an install without the extra.
    code = (
        "import sys; sys.modules['pandas'] = None\n"
        "import nordic_balancing\n"
        "assert 'to_frame' in nordic_balancing.__all__\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
