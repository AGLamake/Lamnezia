import pytest

from amnezia_core.awg_install import generate_awg_params
from amnezia_core.awg_params import ParamProblem, Problem, check_awg_params

# The parameters of a server set up by the Amnezia app (tests/fixtures/awg0.conf).
APP_PARAMS = {
    "Jc": "4",
    "Jmin": "10",
    "Jmax": "50",
    "S1": "72",
    "S2": "56",
    "S3": "32",
    "S4": "16",
    "H1": "1020325451-1025713284",
    "H2": "1183243451-1193573213",
    "H3": "1451245281-1471212327",
    "H4": "1785210913-1795201230",
    "I1": "<r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001c00c000100010000105a>",
}


def problems(**changes: str) -> list[ParamProblem]:
    return check_awg_params({**APP_PARAMS, **changes})


def test_settings_of_the_app_and_of_the_installer_are_valid() -> None:
    assert check_awg_params(APP_PARAMS) == []
    assert check_awg_params(generate_awg_params()) == []


def test_every_parameter_may_be_empty() -> None:
    assert check_awg_params({}) == []
    assert check_awg_params(dict.fromkeys(APP_PARAMS, " ")) == []


@pytest.mark.parametrize(
    ("param", "value", "problem"),
    [
        ("Jc", "four", Problem.NOT_A_NUMBER),
        ("Jc", "-1", Problem.NOT_A_NUMBER),
        ("Jc", "129", Problem.TOO_LARGE),
        ("S1", "1133", Problem.TOO_LARGE),
        ("S4", "1249", Problem.TOO_LARGE),
        ("H2", "5-", Problem.NOT_A_RANGE),
        ("H2", "9-5", Problem.NOT_A_RANGE),
        ("H2", "4294967296", Problem.NOT_A_RANGE),
        ("RekeyAfterTime", "soon", Problem.NOT_A_RANGE),
        ("RandomTrailers", "yes", Problem.NOT_A_TOGGLE),
        ("HeaderProtectionKey", "c2hvcnQ=", Problem.NOT_A_KEY),
        ("I1", "<x 1>", Problem.NOT_JUNK_TAGS),
        ("I3", "plain text", Problem.NOT_JUNK_TAGS),
    ],
)
def test_wrong_format(param: str, value: str, problem: Problem) -> None:
    assert problems(**{param: value}) == [ParamProblem(param, problem)]


def test_minimum_junk_size_above_maximum() -> None:
    assert problems(Jmin="60") == [ParamProblem("Jmin", Problem.MIN_ABOVE_MAX)]
    assert problems(Jmin="50") == []


def test_magic_headers_must_differ_and_not_overlap() -> None:
    assert check_awg_params({"H1": "1", "H2": "1"}) == [ParamProblem("H1", Problem.HEADERS_OVERLAP)]
    assert problems(H3="1025713284-1100000000") == [ParamProblem("H1", Problem.HEADERS_OVERLAP)]
    assert check_awg_params({"H1": "1", "H2": "2", "H3": "3-9", "H4": "10"}) == []


def test_junk_must_not_make_two_messages_equally_long() -> None:
    # S1 + 148 == S2 + 92: the same rule as upstream isPacketSizeEqual.
    assert problems(S1="0", S2="56") == [ParamProblem("S1", Problem.SIZES_COLLIDE)]


def test_header_protection_needs_padding_of_twelve_bytes() -> None:
    key = generate_awg_params()["HeaderProtectionKey"]

    assert problems(HeaderProtectionKey=key, S2="11", S4="") == [
        ParamProblem("S2", Problem.PADDING_TOO_SMALL),
        ParamProblem("S4", Problem.PADDING_TOO_SMALL),
    ]
    assert problems(HeaderProtectionKey=key, S1="12", S2="13", S3="14", S4="15") == []


def test_padding_below_twelve_is_fine_without_header_protection() -> None:
    assert problems(S1="0", S2="5", S3="", S4="") == []


def test_a_relation_is_not_reported_on_top_of_a_wrong_value() -> None:
    assert problems(Jmin="x", Jmax="5") == [ParamProblem("Jmin", Problem.NOT_A_NUMBER)]
