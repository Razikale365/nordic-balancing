"""Offline memory and timing benchmark for one year of imbalance prices.

Each scenario fetches a full year for all 12 bidding zones through
``httpx.MockTransport``; no network access happens. eSett responses are JSON
arrays of records shaped like the ones in ``tests/test_esett.py``. ENTSO-E
responses are ZIP bodies holding one schema-4.4 ``Balancing_MarketDocument``
per zone and chunk, ``curveType`` A03 with two ``TimeSeries`` per ``Period``
(categories A04 and A05 at equal prices), matching the observed documents in
``docs/SOURCE_NOTES.md`` and built like ``tests/test_entsoe.py``.

Four scenarios, all 12 zones:

  esett-2025    2025-01-01 to 2026-01-01, one 15-minute record per interval
  esett-2022    2022-01-01 to 2023-01-01, one hourly record expanded to quarters
  entsoe-2025   2025-01-01 to 2026-01-01, PT15M points in two categories
  entsoe-2022   2022-01-01 to 2023-01-01, PT60M points in two categories

Every scenario runs in a fresh subprocess (the ``--child`` mode below), three
times by default, and the median of each measure is reported:

  peak RSS        largest resident set of the child process, in bytes; RSS is
                  measured with psutil because tracemalloc misses allocations
                  made inside expat (the C XML parser)
  traced peak     tracemalloc peak during the fetch call
  retained        tracemalloc current size right after the call, while the
                  result list is still referenced
  wall            seconds for the fetch call, including generation of the
                  synthetic responses inside the mock handler
  bytes/record    peak RSS divided by the number of records returned
  frame +peak     extra traced peak from ``to_frame(records)``
  frame +retained extra traced current size from ``to_frame(records)``

Run: uv run --with psutil python examples/benchmarks/memory.py
"""

import gc
import io
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psutil

from nordic_balancing import INTERVAL, BiddingZone, EntsoeClient, ESettClient, to_frame

_HOUR = timedelta(hours=1)
_API_KEY = "memory-benchmark"  # synthetic token; MockTransport answers every request
_NS_44 = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:4"
_ESETT_ZONES = {  # response "mba" values are the short names, requests send these EICs
    "10YDK-1--------W": BiddingZone.DK1,
    "10YDK-2--------M": BiddingZone.DK2,
    "10YFI_1________U": BiddingZone.FI,
    "10YNO_1________2": BiddingZone.NO1,
    "10YNO_2________T": BiddingZone.NO2,
    "10YNO_3________J": BiddingZone.NO3,
    "10YNO_4________9": BiddingZone.NO4,
    "10Y1001A1001A48H": BiddingZone.NO5,
    "10Y1001A1001A44P": BiddingZone.SE1,
    "10Y1001A1001A45N": BiddingZone.SE2,
    "10Y1001A1001A46L": BiddingZone.SE3,
    "10Y1001A1001A47J": BiddingZone.SE4,
}
_ESETT_PRICES: dict[str, Any] = {  # shape copied from tests/test_esett.py PUBLISHED
    "imblSalesPrice": 21.0,
    "imblPurchasePrice": 21.0,
    "upRegPrice": 76.32,
    "downRegPrice": 21.0,
    "mainDirRegPowerPerMBA": -1.0,
    "imblSpotDifferencePrice": -55.32,
    "upRegPriceFrrA": None,
}
SCENARIOS: dict[str, tuple[str, datetime, datetime, timedelta]] = {
    "esett-2025": (
        "esett",
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
        INTERVAL,
    ),
    "esett-2022": (
        "esett",
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2023, 1, 1, tzinfo=UTC),
        _HOUR,
    ),
    "entsoe-2025": (
        "entsoe",
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
        INTERVAL,
    ),
    "entsoe-2022": (
        "entsoe",
        datetime(2022, 1, 1, tzinfo=UTC),
        datetime(2023, 1, 1, tzinfo=UTC),
        _HOUR,
    ),
}
_METRICS = (
    "records",
    "wall_seconds",
    "frame_wall_seconds",
    "peak_rss_bytes",
    "traced_peak_bytes",
    "retained_bytes",
    "frame_extra_peak_bytes",
    "frame_extra_retained_bytes",
)


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%MZ")


def _esett_body(request: httpx.Request, step: timedelta) -> bytes:
    start = datetime.fromisoformat(request.url.params["start"])
    end = datetime.fromisoformat(request.url.params["end"])
    zones = [_ESETT_ZONES[eic] for eic in request.url.params.get_list("mba")]
    records = []
    current = start
    while current < end:
        stamp = current.isoformat().replace("+00:00", "Z")
        local = (current + _HOUR).strftime("%Y-%m-%dT%H:%M:%S")
        records.extend(
            {"timestamp": local, "timestampUTC": stamp, "mba": zone.value, **_ESETT_PRICES}
            for zone in zones
        )
        current += step
    return json.dumps(records).encode()


def _esett_client(step: timedelta) -> ESettClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_esett_body(request, step))

    return ESettClient(httpx.Client(transport=httpx.MockTransport(handler)))


def _entsoe_body(request: httpx.Request, resolution: str) -> bytes:
    eic = request.url.params["controlArea_Domain"]
    start = datetime.strptime(request.url.params["periodStart"], "%Y%m%d%H%M").replace(tzinfo=UTC)
    end = datetime.strptime(request.url.params["periodEnd"], "%Y%m%d%H%M").replace(tzinfo=UTC)
    step = _HOUR if resolution == "PT60M" else INTERVAL
    count = int((end - start) // step)
    period_head = (
        f"<Period><timeInterval><start>{_stamp(start)}</start>"
        f"<end>{_stamp(end)}</end></timeInterval>"
        f"<resolution>{resolution}</resolution>"
    )
    parts = [
        f'<Balancing_MarketDocument xmlns="{_NS_44}">',
        "<type>A85</type>",
        f'<area_Domain.mRID codingScheme="A01">{eic}</area_Domain.mRID>',
    ]
    for category in ("A04", "A05"):
        points = "".join(
            f"<Point><position>{position}</position>"
            f"<imbalance_Price.amount>21.0</imbalance_Price.amount>"
            f"<imbalance_Price.category>{category}</imbalance_Price.category></Point>"
            for position in range(1, count + 1)
        )
        parts.append(
            "<TimeSeries><currency_Unit.name>EUR</currency_Unit.name>"
            "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
            f"<curveType>A03</curveType>{period_head}{points}</Period></TimeSeries>"
        )
    parts.append("</Balancing_MarketDocument>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("document.xml", "".join(parts))
    return buffer.getvalue()


def _entsoe_client(step: timedelta) -> EntsoeClient:
    resolution = "PT60M" if step == _HOUR else "PT15M"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_entsoe_body(request, resolution),
            headers={"Content-Type": "application/zip"},
        )

    return EntsoeClient(_API_KEY, httpx.Client(transport=httpx.MockTransport(handler)))


def _sample_rss(process: psutil.Process, samples: list[int], stop: threading.Event) -> None:
    while not stop.wait(0.01):
        samples.append(process.memory_info().rss)


def _peak_rss(process: psutil.Process, samples: list[int]) -> int:
    """Best available peak RSS of this process in bytes."""
    info = process.memory_info()
    candidates = [info.rss, max(samples, default=0)]
    peak_wset = getattr(info, "peak_wset", None)  # Windows: kernel-tracked peak
    if peak_wset:
        candidates.append(int(peak_wset))
    try:
        import resource
    except ImportError:
        pass
    else:  # Unix: kernel-tracked peak RSS, in KiB except on macOS where it is bytes
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        candidates.append(peak if sys.platform == "darwin" else peak * 1024)
    return max(candidates)


def _child(scenario: str) -> dict[str, Any]:
    source, start, end, step = SCENARIOS[scenario]
    process = psutil.Process()
    samples: list[int] = []
    stop = threading.Event()
    sampler = threading.Thread(target=_sample_rss, args=(process, samples, stop), daemon=True)
    sampler.start()
    client = _esett_client(step) if source == "esett" else _entsoe_client(step)
    to_frame([])  # warms the lazy pandas import so its modules are not counted
    gc.collect()
    tracemalloc.start()
    began = time.perf_counter()
    prices = client.imbalance_prices(start, end)
    wall_seconds = time.perf_counter() - began
    retained, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    began = time.perf_counter()
    frame = to_frame(prices)
    frame_wall_seconds = time.perf_counter() - began
    frame_total_retained, frame_total_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stop.set()
    sampler.join()
    return {
        "records": len(prices),
        "frame_columns": len(frame.columns),
        "wall_seconds": wall_seconds,
        "frame_wall_seconds": frame_wall_seconds,
        "peak_rss_bytes": _peak_rss(process, samples),
        "traced_peak_bytes": traced_peak,
        "retained_bytes": retained,
        "frame_extra_peak_bytes": frame_total_peak - retained,
        "frame_extra_retained_bytes": frame_total_retained - retained,
    }


def _run_child_once(scenario: str) -> dict[str, Any]:
    command = [sys.executable, str(Path(__file__).resolve()), "--child", scenario]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"child for {scenario} failed:\n{completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _machine_info() -> str:
    return (
        f"{platform.platform()}, {os.cpu_count()} CPUs, "
        f"{psutil.virtual_memory().total / 2**30:.1f} GiB RAM, "
        f"Python {platform.python_version()}"
    )


def _row(name: str, result: dict[str, Any]) -> str:
    mib = 2**20
    peak_rss = result["peak_rss_bytes"]
    threshold = []
    if peak_rss > 1536 * mib:
        threshold.append("peak RSS")
    if peak_rss / result["records"] > 3 * 1024:
        threshold.append("bytes/record")
    return (
        f"| {name} | {result['records']:,} | {result['wall_seconds']:.1f} | "
        f"{peak_rss / mib:,.0f} | {result['traced_peak_bytes'] / mib:,.0f} | "
        f"{result['retained_bytes'] / mib:,.0f} | "
        f"{result['frame_extra_peak_bytes'] / mib:,.0f} | "
        f"{result['frame_extra_retained_bytes'] / mib:,.0f} | "
        f"{peak_rss / result['records']:,.0f} | "
        f"{', '.join(threshold) if threshold else 'not crossed'} |"
    )


def main(argv: list[str]) -> int:
    if argv[:1] == ["--child"] and len(argv) == 2 and argv[1] in SCENARIOS:
        print(json.dumps(_child(argv[1])))
        return 0
    names = list(argv) or list(SCENARIOS)
    if unknown := set(names) - set(SCENARIOS):
        sys.exit(f"unknown scenarios: {sorted(unknown)}; choose from {sorted(SCENARIOS)}")
    print(f"machine: {_machine_info()}")
    print(
        "| scenario | records | wall s | peak RSS MiB | traced peak MiB | retained MiB "
        "| frame +peak MiB | frame +retained MiB | B/record | threshold crossed |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for name in names:
        results = [_run_child_once(name) for _ in range(3)]
        for index, result in enumerate(results):
            print(f"{name} run {index + 1}/3: {json.dumps(result)}", flush=True)
        median = {key: statistics.median(r[key] for r in results) for key in _METRICS}
        print(_row(name, median), flush=True)
    print("wall seconds measured under load; medians over 3 fresh subprocesses")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
