"""Offline reproduction of entsoe-py issue #529 on entsoe-py 0.8.1.

``EntsoePandasClient.query_imbalance_prices`` is wrapped in the ``year_limited``
decorator (entsoe/decorators.py), which splits multi-year requests into yearly
blocks. For every block after the first it keeps only ``frame.index > _start``,
while the ENTSO-E API treats ``periodEnd`` as exclusive — so the first row of
each non-first block is silently dropped. One row is lost per year boundary.

This script serves synthetic A85 (Balancing_MarketDocument) responses to both
libraries for SE_3 / SE3, start 2023-12-30, end 2025-01-03 (Europe/Stockholm).
Each response contains exactly the 15-minute points in [periodStart, periodEnd)
with a deterministic price per timestamp. No network access is used.

Run: uv run --with entsoe-py==0.8.1 python examples/entsoe_py_comparison/repro_issue_529.py
"""

import io
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from entsoe import EntsoePandasClient

from nordic_balancing import EntsoeClient
from nordic_balancing.models import BiddingZone

NS = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:0"
LOCAL = ZoneInfo("Europe/Stockholm")
START = datetime(2023, 12, 30, tzinfo=LOCAL)
END = datetime(2025, 1, 3, tzinfo=LOCAL)
QUARTER = timedelta(minutes=15)


def price_at(moment: datetime) -> float:
    """Deterministic price for a UTC timestamp (EUR/MWh)."""
    return round((int(moment.timestamp()) // 900) % 10_000) / 10 + 1.0


def a85_zip(start: datetime, end: datetime) -> bytes:
    """ZIP holding one A85 XML with every 15-minute point in [start, end)."""
    points = []
    position = 0
    cursor = start
    while cursor < end:
        position += 1
        points.append(
            f"<Point><position>{position}</position>"
            f"<imbalance_Price.amount>{price_at(cursor)}</imbalance_Price.amount>"
            f"<imbalance_Price.category>A04</imbalance_Price.category></Point>"
        )
        cursor += QUARTER
    fmt = "%Y-%m-%dT%H:%MZ"
    xml = (
        f'<Balancing_MarketDocument xmlns="{NS}">'
        "<TimeSeries>"
        "<currency_Unit.name>EUR</currency_Unit.name>"
        "<curveType>A01</curveType>"
        "<Period>"
        f"<timeInterval><start>{start.strftime(fmt)}</start>"
        f"<end>{end.strftime(fmt)}</end></timeInterval>"
        "<resolution>PT15M</resolution>"
        f"{''.join(points)}"
        "</Period>"
        "</TimeSeries>"
        "</Balancing_MarketDocument>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("member0.xml", xml)
    return buffer.getvalue()


def _parse_param(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=UTC)


def window(params: Mapping[str, str]) -> tuple[datetime, datetime]:
    return _parse_param(params["periodStart"]), _parse_param(params["periodEnd"])


class FakeResponse:
    """Minimal stand-in for requests.Response carrying a ZIP body."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers = {"content-type": "application/octet-stream"}
        self.text = ""

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    """Stand-in for requests.Session; answers A85 queries from a85_zip."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, params=None, **kwargs: object) -> FakeResponse:
        assert params is not None
        assert params["documentType"] == "A85"
        start, end = window(params)
        self.calls.append((params["periodStart"], params["periodEnd"]))
        return FakeResponse(a85_zip(start, end))


def run_entsoe_py() -> tuple[pd.DatetimeIndex, list[tuple[str, str]]]:
    session = FakeSession()
    client = EntsoePandasClient(api_key="offline-dummy", session=session)
    df = client.query_imbalance_prices("SE_3", start=pd.Timestamp(START), end=pd.Timestamp(END))
    return pd.DatetimeIndex(df.index), session.calls


def run_nordic_balancing() -> list[datetime]:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        start, end = window(params)
        return httpx.Response(200, content=a85_zip(start, end))

    client = EntsoeClient(
        api_key="offline-dummy",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    prices = client.imbalance_prices(START, END, zones=[BiddingZone.SE3])
    return [p.start for p in prices]


def main() -> None:
    expected_local = pd.date_range(
        start=pd.Timestamp(START), end=pd.Timestamp(END), freq=QUARTER, inclusive="left"
    )
    print("Window:", START.isoformat(), "to", END.isoformat(), "(Europe/Stockholm)")
    print("Expected 15-minute rows:", len(expected_local))
    print()

    index, calls = run_entsoe_py()
    missing_py = expected_local.difference(index)
    print("entsoe-py 0.8.1 EntsoePandasClient.query_imbalance_prices('SE_3'):")
    print("  block queries issued (periodStart -> periodEnd, UTC):")
    for period_start, period_end in calls:
        print(f"    {period_start} -> {period_end}")
    print("  returned rows:", len(index))
    print("  missing timestamps:", [ts.isoformat() for ts in missing_py])
    reproduced = len(missing_py) > 0
    print(
        "  => issue #529",
        "REPRODUCED: one row lost per year-block boundary"
        if reproduced
        else "did NOT reproduce on entsoe-py 0.8.1",
    )
    print()

    nordic_index = run_nordic_balancing()
    expected_utc = {ts.astimezone(UTC) for ts in expected_local}
    missing_nb = sorted(expected_utc.difference(nordic_index))
    print("nordic-balancing EntsoeClient.imbalance_prices(SE3), same synthetic server:")
    print("  returned rows:", len(nordic_index))
    print("  missing timestamps:", [ts.isoformat() for ts in missing_nb])
    print(
        "  =>",
        "complete" if not missing_nb else f"LOST {len(missing_nb)} rows",
    )


if __name__ == "__main__":
    main()
