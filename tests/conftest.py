import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def fingrid_live_key(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("live") is None or "FINGRID_API_KEY" in os.environ:
        return
    dotenv = Path(__file__).resolve().parents[1] / ".env"
    if not dotenv.is_file():
        return
    for line in dotenv.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if not separator or name.strip() != "FINGRID_API_KEY":
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
            os.environ.setdefault("FINGRID_API_KEY", value)
        return
