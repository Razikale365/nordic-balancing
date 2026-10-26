"""Shared check for a credential echoed into a credentialed client's response.

A 200 body becomes returned data, so a body that carries the key is not an
answer to return. ``echoes_key`` is the byte scan run on every response; only a
body that could hide the key behind a decoding layer — a JSON escape, an XML
reference, comment, CDATA section or processing instruction, or the zero bytes
of UTF-16/32 — pays for the decoded-text search in
``json_echoes_key``/``xml_echoes_key``.
"""

import json
import re
from collections.abc import Iterator
from functools import lru_cache
from urllib.parse import quote
from xml.etree import ElementTree


def echoes_key(body: bytes, api_key: str) -> bool:
    """Whether the body bytes carry the key raw or percent-encoded, in any case."""
    if not api_key:
        return False
    lowered = body.lower()
    if api_key.encode().lower() in lowered:
        return True
    if quote(api_key, safe="").lower().encode() in lowered:
        return True
    # A spelling mixing raw and escaped characters (say "tok%65n") can only
    # exist where a percent sign does; without one the pattern cannot match.
    return b"%" in body and _bytes_pattern(api_key).search(body) is not None


def json_can_hide_key(body: bytes) -> bool:
    """Whether JSON decoding could still reveal the key: any escape, or a wide encoding."""
    return b"\\" in body or b"\x00" in body


def xml_can_hide_key(body: bytes) -> bool:
    """Whether XML parsing could still reveal the key inside ``body``.

    References and entities, comments, CDATA sections and processing instructions
    (beyond the leading declaration) all split or encode text that the parser
    joins back, and a wide encoding hides it from the byte scan.
    """
    return (
        b"&" in body
        or b"<!--" in body
        or b"<![CDATA[" in body
        or body.find(b"<?", 1) != -1
        or b"\x00" in body
    )


def json_echoes_key(body: bytes, api_key: str) -> bool:
    """Whether a string or key of the decoded JSON document holds the key."""
    try:
        document = json.loads(body)  # bytes: the codec is detected for us
    except (ValueError, RecursionError):
        # Not JSON: the body is rejected as unparseable before it reaches raw.
        return False
    return _text_pattern(api_key).search("\x00".join(_strings(document))) is not None


def xml_echoes_key(root: ElementTree.Element, api_key: str) -> bool:
    """Whether an element's text, tail or attribute value holds the key."""
    return _text_pattern(api_key).search("\x00".join(_xml_strings(root))) is not None


def _strings(document: object) -> Iterator[str]:
    # Iterative, so a deeply nested body cannot exhaust the recursion limit here.
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            stack.extend(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _xml_strings(root: ElementTree.Element) -> Iterator[str]:
    for element in root.iter():
        if element.text:
            yield element.text
        if element.tail:
            yield element.tail
        yield from element.attrib.values()


def _key_source(api_key: str) -> str:
    # The key is printable ASCII, so each character is one byte. An echo may
    # carry each of them raw or percent-escaped in either hex case, like a URL —
    # the same shapes _redact covers in entsoe.py.
    parts = []
    for char in api_key:
        escaped = "%" + "".join(f"[{d}{d.lower()}]" for d in f"{ord(char):02X}")
        parts.append(f"(?:{escaped}|{re.escape(char)})")
    return "".join(parts)


@lru_cache(maxsize=32)
def _bytes_pattern(api_key: str) -> re.Pattern[bytes]:
    return re.compile(_key_source(api_key).encode("ascii"), re.IGNORECASE)


@lru_cache(maxsize=32)
def _text_pattern(api_key: str) -> re.Pattern[str]:
    # The key is ASCII, so ASCII case folding is enough and no wider Unicode
    # equivalence (a Kelvin sign for k, say) may alias-match a key character.
    return re.compile(_key_source(api_key), re.IGNORECASE | re.ASCII)
