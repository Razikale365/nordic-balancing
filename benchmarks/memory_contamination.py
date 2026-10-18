"""How much of examples/benchmarks/memory.py's reported wall time is payload
generation inside the timed region?

``memory.py`` builds every synthetic response inside the ``MockTransport``
handler, which runs inside the timed ``imbalance_prices`` call. This driver
imports the example, wraps its body generators with a counter, and runs its
own ``_child`` in-process per scenario, reporting handler time versus wall.

  uv run --group benchmarks python benchmarks/memory_contamination.py [scenario ...]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "benchmarks"))
import memory


def _timed(fn, acc: dict):
    def wrapper(*args, **kwargs):
        began = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            acc["seconds"] += time.perf_counter() - began
            acc["calls"] += 1

    return wrapper


def main(argv: list[str]) -> int:
    names = argv or list(memory.SCENARIOS)
    for name in names:
        source = memory.SCENARIOS[name][0]
        acc = {"seconds": 0.0, "calls": 0}
        if source == "esett":
            memory._esett_body = _timed(memory._esett_body, acc)
        else:
            memory._entsoe_body = _timed(memory._entsoe_body, acc)
        began = time.perf_counter()
        result = memory._child(name)
        total = time.perf_counter() - began
        print(
            f"{name}: handler={acc['seconds']:.1f}s over {acc['calls']} calls "
            f"({100 * acc['seconds'] / result['wall_seconds']:.0f}% of the "
            f"{result['wall_seconds']:.1f}s timed fetch; child total {total:.1f}s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
