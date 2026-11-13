import pytest

from amnezia_core.errors import DumpFormatError
from amnezia_core.xray_stats import XrayClientTraffic, parse_stats

ALICE = "a" * 64
BOB = "b" * 64


def test_counters_of_clients() -> None:
    text = f"""{{
      "stat": [
        {{"name": "user>>>{ALICE}>>>traffic>>>uplink", "value": 746}},
        {{"name": "user>>>{ALICE}>>>traffic>>>downlink", "value": 3010263}},
        {{"name": "user>>>{BOB}>>>traffic>>>downlink", "value": "2003533"}},
        {{"name": "inbound>>>api>>>traffic>>>uplink", "value": 10}}
      ]
    }}"""

    assert parse_stats(text) == [
        XrayClientTraffic(ALICE, rx_bytes=746, tx_bytes=3010263),
        XrayClientTraffic(BOB, rx_bytes=0, tx_bytes=2003533),
    ]


def test_counter_at_zero_has_no_value() -> None:
    text = f'{{"stat": [{{"name": "user>>>{ALICE}>>>traffic>>>uplink"}}]}}'

    assert parse_stats(text) == [XrayClientTraffic(ALICE)]


def test_no_counters_yet() -> None:
    assert parse_stats("{}") == []


@pytest.mark.parametrize(
    "text",
    [
        "failed to dial 127.0.0.1:10085",
        "[]",
        '{"stat": {}}',
        '{"stat": [{"value": 1}]}',
        f'{{"stat": [{{"name": "user>>>{ALICE}>>>traffic>>>uplink", "value": -1}}]}}',
        f'{{"stat": [{{"name": "user>>>{ALICE}>>>traffic>>>uplink", "value": true}}]}}',
    ],
)
def test_unexpected_output_is_refused(text: str) -> None:
    with pytest.raises(DumpFormatError):
        parse_stats(text)
