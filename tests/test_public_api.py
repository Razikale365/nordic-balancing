import dataclasses
import inspect
import json
import os
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

import nordic_balancing
from nordic_balancing.sources import (
    energidataservice,
    entsoe,
    esett,
    fingrid,
    svk,
)

SNAPSHOT_PATH = Path(__file__).with_name("api_snapshot.json")
REGENERATE_CMD = "UPDATE_API_SNAPSHOT=1 uv run pytest tests/test_public_api.py"
_CLIENT_MODULES = (energidataservice, entsoe, esett, fingrid, svk)


def _format_field(field: dataclasses.Field[Any]) -> str:
    annotation = inspect.formatannotation(field.type)
    if field.default is not dataclasses.MISSING:
        return f"{field.name}: {annotation} = {field.default!r}"
    return f"{field.name}: {annotation}"


def _public_methods(cls: type[Any], *, include_dunders: bool) -> dict[str, str]:
    dunders = ("__init__", "__enter__", "__exit__") if include_dunders else ()
    return {
        attr: str(inspect.signature(getattr(cls, attr)))
        for attr, val in sorted(cls.__dict__.items())
        if (not attr.startswith("_") or attr in dunders) and callable(val)
    }


def _public_properties(cls: type[Any]) -> dict[str, str]:
    return {
        attr: str(inspect.signature(val.fget))
        for attr, val in sorted(cls.__dict__.items())
        if not attr.startswith("_") and isinstance(val, property) and val.fget is not None
    }


def build_public_api_snapshot() -> dict[str, Any]:
    symbols: dict[str, Any] = {}
    for name in sorted(nordic_balancing.__all__):
        obj = getattr(nordic_balancing, name)
        if isinstance(obj, type) and issubclass(obj, Enum):
            symbols[name] = {
                "kind": "enum",
                "members": {member.name: member.value for member in obj},
            }
        elif dataclasses.is_dataclass(obj) and isinstance(obj, type):
            symbols[name] = {
                "fields": [_format_field(field) for field in dataclasses.fields(obj)],
                "kind": "dataclass",
                "methods": _public_methods(obj, include_dunders=False),
                "properties": _public_properties(obj),
            }
        elif isinstance(obj, type):
            symbols[name] = {
                "bases": [base.__name__ for base in obj.__bases__],
                "kind": "class",
                "methods": _public_methods(obj, include_dunders=True),
                "properties": _public_properties(obj),
            }
        elif callable(obj):
            symbols[name] = {
                "kind": "function",
                "signature": str(inspect.signature(obj)),
            }
        else:
            symbols[name] = {
                "kind": "constant",
                "type": type(obj).__name__,
            }
    return {
        "__all__": sorted(nordic_balancing.__all__),
        "sources": {
            module.__name__.rpartition(".")[2]: module.SOURCE for module in _CLIENT_MODULES
        },
        "symbols": symbols,
    }


def _diff_message(expected: dict[str, Any], current: dict[str, Any]) -> str:
    expected_symbols: dict[str, Any] = expected.get("symbols", {})
    current_symbols: dict[str, Any] = current.get("symbols", {})
    expected_sources: dict[str, Any] = expected.get("sources", {})
    current_sources: dict[str, Any] = current.get("sources", {})

    removed = sorted(expected_symbols.keys() - current_symbols.keys()) + sorted(
        f"sources.{k}" for k in expected_sources.keys() - current_sources.keys()
    )
    changed = sorted(
        k
        for k in expected_symbols.keys() & current_symbols.keys()
        if expected_symbols[k] != current_symbols[k]
    ) + sorted(
        f"sources.{k}"
        for k in expected_sources.keys() & current_sources.keys()
        if expected_sources[k] != current_sources[k]
    )
    added = sorted(current_symbols.keys() - expected_symbols.keys()) + sorted(
        f"sources.{k}" for k in current_sources.keys() - expected_sources.keys()
    )
    if expected.get("__all__") != current.get("__all__") and not (removed or added):
        changed.append("__all__")

    lines = ["Public API surface does not match tests/api_snapshot.json."]
    if removed or changed:
        lines.append(f"Removed or changed: {', '.join(removed + changed)}")
    if added:
        lines.append(f"Added: {', '.join(added)}")
    lines.append(f"Regenerate with `{REGENERATE_CMD}`.")
    return "\n".join(lines)


def test_public_api_snapshot() -> None:
    current = build_public_api_snapshot()
    rendered = json.dumps(current, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_API_SNAPSHOT") == "1":
        SNAPSHOT_PATH.write_text(rendered, encoding="utf-8")
    if not SNAPSHOT_PATH.exists():
        pytest.fail(f"Missing {SNAPSHOT_PATH}. Regenerate with `{REGENERATE_CMD}`.")
    raw = SNAPSHOT_PATH.read_text(encoding="utf-8")
    expected: dict[str, Any] = json.loads(raw)
    if current != expected or raw != rendered:
        pytest.fail(_diff_message(expected, current))


def test_public_exports_invariants() -> None:
    assert len(nordic_balancing.__all__) == len(set(nordic_balancing.__all__))
    for name in nordic_balancing.__all__:
        assert not (name.startswith("_") and not name.startswith("__")), name
        obj = getattr(nordic_balancing, name)
        assert not inspect.ismodule(obj), name
        if isinstance(obj, type) and issubclass(obj, BaseException):
            assert issubclass(obj, nordic_balancing.NordicBalancingError), name


def test_diff_message_lists_removed_or_changed_separately_from_added() -> None:
    baseline: dict[str, Any] = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    modified: dict[str, Any] = json.loads(json.dumps(baseline))
    del modified["symbols"]["to_frame"]
    modified["__all__"].remove("to_frame")
    modified["symbols"]["CHANGES"]["type"] = "list"
    modified["symbols"]["new_helper"] = {"kind": "function", "signature": "() -> None"}
    modified["__all__"].append("new_helper")

    message = _diff_message(baseline, modified)
    assert "Removed or changed: to_frame, CHANGES" in message
    assert "Added: new_helper" in message
    assert REGENERATE_CMD in message
