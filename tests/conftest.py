import os
from pathlib import Path

import pytest

_LIVE_KEYS = ("FINGRID_API_KEY", "ENTSOE_API_KEY")


@pytest.fixture(autouse=True)
def live_keys(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("live") is None:
        return
    needed = {key for key in _LIVE_KEYS if key not in os.environ}
    if not needed:
        return
    dotenv = Path(__file__).resolve().parents[1] / ".env"
    if not dotenv.is_file():
        return
    for line in dotenv.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        name = name.strip()
        if not separator or name not in needed:
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing < 0:
                continue
            value = value[1:closing]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if value:
            os.environ.setdefault(name, value)
            needed.discard(name)
        if not needed:
            return
