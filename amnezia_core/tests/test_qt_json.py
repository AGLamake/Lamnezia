from typing import Any

import pytest

from amnezia_core.qt_json import to_qt_json


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([], "[\n]\n"),
        ({}, "{\n}\n"),
        ({"b": {}, "a": []}, '{\n    "a": [\n    ],\n    "b": {\n    }\n}\n'),
        ([1.0, 1.5, 2, None, False], "[\n    1,\n    1.5,\n    2,\n    null,\n    false\n]\n"),
        (['"q" \\ \n\t\u0001 я'], '[\n    "\\"q\\" \\\\ \\n\\t\\u0001 я"\n]\n'),
    ],
)
def test_matches_qt_indented_format(value: Any, expected: str) -> None:
    assert to_qt_json(value) == expected
