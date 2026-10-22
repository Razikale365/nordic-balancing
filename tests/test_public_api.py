import dataclasses
import inspect
import json
import os
import re
from datetime import datetime
from enum import Enum, IntEnum
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
_DUNDERS = ("__init__", "__enter__", "__exit__")


def _format_field(field: dataclasses.Field[Any]) -> str:
    annotation = inspect.formatannotation(field.type)
    flags = f"(compare={field.compare}, repr={field.repr})"
    if field.default is not dataclasses.MISSING:
        return f"{field.name}: {annotation} = {field.default!r} {flags}"
    if field.default_factory is not dataclasses.MISSING:
        name = getattr(field.default_factory, "__qualname__", repr(field.default_factory))
        return f"{field.name}: {annotation} = <default_factory {name}> {flags}"
    return f"{field.name}: {annotation} {flags}"


def _is_method_descriptor(val: Any) -> bool:
    return isinstance(val, classmethod | staticmethod) or callable(val)


def _public_methods(cls: type[Any]) -> dict[str, str]:
    return {
        attr: str(inspect.signature(getattr(cls, attr)))
        for attr, val in sorted(cls.__dict__.items())
        if (not attr.startswith("_") or attr in _DUNDERS) and _is_method_descriptor(val)
    }


def _public_properties(cls: type[Any]) -> dict[str, str]:
    return {
        attr: str(inspect.signature(val.fget))
        for attr, val in sorted(cls.__dict__.items())
        if not attr.startswith("_") and isinstance(val, property) and val.fget is not None
    }


def _public_class_attributes(cls: type[Any]) -> dict[str, str]:
    field_names = (
        {field.name for field in dataclasses.fields(cls)}
        if dataclasses.is_dataclass(cls)
        else set()
    )
    member_names = set(cls.__members__) if issubclass(cls, Enum) else set()
    return {
        attr: type(val).__name__
        for attr, val in sorted(cls.__dict__.items())
        if not attr.startswith("_")
        and attr not in field_names
        and attr not in member_names
        and not _is_method_descriptor(val)
        and not isinstance(val, property)
    }


def _dataclass_params(cls: type[Any]) -> dict[str, Any]:
    params = cls.__dataclass_params__
    found = {name: getattr(params, name) for name in type(params).__slots__}
    # Python 3.11 does not record these four in __dataclass_params__; read them off
    # the class so the snapshot is the same on every supported version.
    slots = cls.__dict__.get("__slots__")
    derived = {
        "kw_only": all(f.kw_only for f in dataclasses.fields(cls)),
        "match_args": "__match_args__" in cls.__dict__,
        "slots": slots is not None,
        "weakref_slot": slots is not None and "__weakref__" in slots,
    }
    return {**derived, **found}


def build_public_api_snapshot() -> dict[str, Any]:
    symbols: dict[str, Any] = {}
    for name in sorted(nordic_balancing.__all__):
        obj = getattr(nordic_balancing, name)
        if isinstance(obj, type) and issubclass(obj, Enum):
            symbols[name] = {
                "attributes": _public_class_attributes(obj),
                "bases": [base.__name__ for base in obj.__bases__],
                "kind": "enum",
                "members": {
                    member_name: member.value for member_name, member in obj.__members__.items()
                },
                "methods": _public_methods(obj),
                "properties": _public_properties(obj),
            }
        elif dataclasses.is_dataclass(obj) and isinstance(obj, type):
            symbols[name] = {
                "attributes": _public_class_attributes(obj),
                "bases": [base.__name__ for base in obj.__bases__],
                "fields": [_format_field(field) for field in dataclasses.fields(obj)],
                "frozen": obj.__dataclass_params__.frozen,  # type: ignore[attr-defined]
                "kind": "dataclass",
                "methods": _public_methods(obj),
                "params": _dataclass_params(obj),
                "properties": _public_properties(obj),
                "slots": "__slots__" in obj.__dict__,
            }
        elif isinstance(obj, type):
            symbols[name] = {
                "attributes": _public_class_attributes(obj),
                "bases": [base.__name__ for base in obj.__bases__],
                "kind": "class",
                "methods": _public_methods(obj),
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


def test_snapshot_detects_kw_only_dataclass_enum_alias_and_classmethod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline: dict[str, Any] = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    @dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
    class KwOnlyMarketChange:
        effective: datetime
        zones: frozenset[nordic_balancing.BiddingZone]
        summary: str
        source_url: str

    with monkeypatch.context() as mp:
        mp.setattr(nordic_balancing, "MarketChange", KwOnlyMarketChange)
        current = build_public_api_snapshot()
        assert current != baseline
        assert "Removed or changed: MarketChange" in _diff_message(baseline, current)

    class AliasedDirection(IntEnum):
        DOWN = -1
        NONE = 0
        OFF = 0
        UP = 1

    with monkeypatch.context() as mp:
        mp.setattr(nordic_balancing, "Direction", AliasedDirection)
        current = build_public_api_snapshot()
        assert current["symbols"]["Direction"]["members"]["OFF"] == 0
        assert "Removed or changed: Direction" in _diff_message(baseline, current)

    with monkeypatch.context() as mp:
        mp.setattr(
            nordic_balancing.EnergiDataServiceClient,
            "from_http",
            classmethod(lambda cls, http: cls(http=http)),
            raising=False,
        )
        current = build_public_api_snapshot()
        assert current["symbols"]["EnergiDataServiceClient"]["methods"]["from_http"] == "(http)"
        assert "Removed or changed: EnergiDataServiceClient" in _diff_message(baseline, current)


def test_snapshot_detects_mutable_unslotted_record_and_enum_base_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline: dict[str, Any] = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert baseline["symbols"]["ImbalancePrice"]["frozen"] is True
    assert baseline["symbols"]["ReserveCapacity"]["slots"] is True
    assert baseline["symbols"]["BiddingZone"]["bases"] == ["StrEnum"]
    assert baseline["symbols"]["Direction"]["bases"] == ["IntEnum"]

    original = nordic_balancing.ImbalancePrice
    fields = [
        (f.name, f.type, dataclasses.field(default=f.default, default_factory=f.default_factory))
        for f in dataclasses.fields(original)
    ]
    mutable = dataclasses.make_dataclass("ImbalancePrice", fields, slots=True)
    unslotted = dataclasses.make_dataclass("ImbalancePrice", fields, frozen=True)
    for replacement, key in ((mutable, "frozen"), (unslotted, "slots")):
        with monkeypatch.context() as mp:
            mp.setattr(nordic_balancing, "ImbalancePrice", replacement)
            current = build_public_api_snapshot()
            assert current["symbols"]["ImbalancePrice"][key] is False
            assert "Removed or changed: ImbalancePrice" in _diff_message(baseline, current)

    members = {m.name: m.value for m in nordic_balancing.BiddingZone}
    plain = Enum("BiddingZone", members)  # type: ignore[misc]
    with monkeypatch.context() as mp:
        mp.setattr(nordic_balancing, "BiddingZone", plain)
        current = build_public_api_snapshot()
        snapshot_members = current["symbols"]["BiddingZone"]["members"]
        assert snapshot_members == baseline["symbols"]["BiddingZone"]["members"]
        assert "Removed or changed: BiddingZone" in _diff_message(baseline, current)


def test_snapshot_detects_dataclass_order_and_base_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline: dict[str, Any] = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert baseline["symbols"]["ImbalancePrice"]["params"]["order"] is False
    assert baseline["symbols"]["ImbalancePrice"]["bases"] == ["object"]

    original = nordic_balancing.ImbalancePrice
    spec = [
        (f.name, f.type, dataclasses.field(compare=False, repr=False) if f.name == "raw" else f)
        for f in dataclasses.fields(original)
    ]
    ordered = dataclasses.make_dataclass(
        "ImbalancePrice", spec, frozen=True, slots=True, order=True
    )
    ordered.end = original.end  # type: ignore[attr-defined]
    with monkeypatch.context() as mp:
        mp.setattr(nordic_balancing, "ImbalancePrice", ordered)
        current = build_public_api_snapshot()
        assert current["symbols"]["ImbalancePrice"]["params"]["order"] is True
        assert "Removed or changed: ImbalancePrice" in _diff_message(baseline, current)

    class Mixin:
        pass

    based = dataclasses.make_dataclass(
        "ImbalancePrice", spec, bases=(Mixin,), frozen=True, slots=True
    )
    based.end = original.end  # type: ignore[attr-defined]
    with monkeypatch.context() as mp:
        mp.setattr(nordic_balancing, "ImbalancePrice", based)
        current = build_public_api_snapshot()
        assert current["symbols"]["ImbalancePrice"]["bases"] == ["Mixin"]
        assert "Removed or changed: ImbalancePrice" in _diff_message(baseline, current)


def test_snapshot_detects_dropped_compare_flag_on_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline: dict[str, Any] = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    raw_field = next(
        item for item in baseline["symbols"]["ImbalancePrice"]["fields"] if item.startswith("raw:")
    )
    assert "(compare=False, repr=False)" in raw_field

    specs = [
        (f.name, f.type, dataclasses.field(repr=False) if f.name == "raw" else f)
        for f in dataclasses.fields(nordic_balancing.ImbalancePrice)
    ]
    weakened = dataclasses.make_dataclass("ImbalancePrice", specs, frozen=True, slots=True)
    with monkeypatch.context() as mp:
        mp.setattr(nordic_balancing, "ImbalancePrice", weakened)
        current = build_public_api_snapshot()
    weakened_raw = next(
        item for item in current["symbols"]["ImbalancePrice"]["fields"] if item.startswith("raw:")
    )
    assert "(compare=True, repr=False)" in weakened_raw
    assert "Removed or changed: ImbalancePrice" in _diff_message(baseline, current)


def test_snapshot_detects_enum_method_property_and_class_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline: dict[str, Any] = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    with monkeypatch.context() as mp:
        mp.setattr(nordic_balancing.BiddingZone, "describe", lambda self: self.value, raising=False)
        mp.setattr(
            nordic_balancing.Direction,
            "label",
            property(lambda self: self.name),
            raising=False,
        )
        current = build_public_api_snapshot()
        assert current["symbols"]["BiddingZone"]["methods"]["describe"] == "(self)"
        assert current["symbols"]["Direction"]["properties"]["label"] == "(self)"
        assert "Removed or changed: BiddingZone, Direction" in _diff_message(baseline, current)

    with monkeypatch.context() as mp:
        mp.setattr(nordic_balancing.ImbalancePrice, "UNIT", "EUR/MWh", raising=False)
        current = build_public_api_snapshot()
        assert current["symbols"]["ImbalancePrice"]["attributes"]["UNIT"] == "str"
        assert "Removed or changed: ImbalancePrice" in _diff_message(baseline, current)


def test_contract_test_references_point_at_existing_tests() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "API_CONTRACT.md").read_text(encoding="utf-8")
    references = set(re.findall(r"tests/(\w+\.py)::(\w+)", text))
    assert references
    missing = []
    for file, name in sorted(references):
        path = root / "tests" / file
        source = path.read_text(encoding="utf-8") if path.is_file() else ""
        if not re.search(rf"^(?:async )?def {name}\(", source, re.MULTILINE):
            missing.append(f"tests/{file}::{name}")
    assert not missing, missing
