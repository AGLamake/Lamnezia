"""JSON text byte-for-byte equal to Qt `QJsonDocument::toJson()` (indented format).

The Amnezia app writes clientsTable this way: 4-space indent, keys sorted, `"key": value`,
empty containers spread over two lines, a newline after the top-level value.
"""

import json
from typing import Any

_MAX_EXACT_FLOAT_INT = 2**53


def to_qt_json(value: Any) -> str:
    return _write(value, 0) + "\n"


def to_qt_compact_json(value: Any) -> str:
    """`QJsonDocument::toJson(Compact)`: sorted keys, no whitespace."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write(value: Any, indent: int) -> str:
    closing = " " * 4 * indent
    inner = " " * 4 * (indent + 1)
    if isinstance(value, dict):
        items = [
            f"{inner}{_scalar(str(key))}: {_write(item, indent + 1)}"
            for key, item in sorted(value.items())
        ]
        return "{\n" + _join(items) + closing + "}"
    if isinstance(value, list):
        return "[\n" + _join([inner + _write(item, indent + 1) for item in value]) + closing + "]"
    return _scalar(value)


def _join(items: list[str]) -> str:
    return ",\n".join(items) + "\n" if items else ""


def _scalar(value: Any) -> str:
    # Qt stores every number as double and prints integral ones without a fraction.
    if isinstance(value, float) and value.is_integer() and abs(value) < _MAX_EXACT_FLOAT_INT:
        return str(int(value))
    return json.dumps(value, ensure_ascii=False)
