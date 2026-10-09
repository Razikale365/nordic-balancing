"""Check the period before analysis.py's window: 2023-05-21T22:00Z to 2024-12-01.

Fetches eSett imbalance prices for all 12 zones (keyless, about 20 chunked requests)
and counts the hours whose four quarter-hour prices differ.

    uv run python examples/mfrr_transition/check_before_window.py
"""

from collections import defaultdict
from datetime import UTC, datetime

from nordic_balancing import BiddingZone, ESettClient

START = datetime(2023, 5, 21, 22, tzinfo=UTC)  # first 15-minute settlement period
END = datetime(2024, 12, 1, tzinfo=UTC)  # where analysis.py starts


def main() -> None:
    with ESettClient() as client:
        prices = client.imbalance_prices(START, END, list(BiddingZone))
    hours: dict[tuple[BiddingZone, datetime], list[float | None]] = defaultdict(list)
    for price in prices:
        hours[(price.zone, price.start.replace(minute=0))].append(price.imbalance_price_eur)
    complete = {key: quarters for key, quarters in hours.items() if None not in quarters}
    complete = {key: quarters for key, quarters in complete.items() if len(quarters) == 4}
    differing = sorted(key for key, quarters in complete.items() if len(set(quarters)) > 1)
    print(f"{len(prices)} records, {len(hours)} zone-hours, {len(complete)} complete")
    print(f"zone-hours with differing quarter prices: {len(differing)}")
    for zone, hour in differing[:10]:
        print(f"  {zone.value} {hour.isoformat()}")


if __name__ == "__main__":
    main()
