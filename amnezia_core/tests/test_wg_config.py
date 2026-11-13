import pytest

from amnezia_core.errors import ConfigFormatError
from amnezia_core.wg_config import Section, WgConfig

MESSY = "# managed by hand\n\n[Interface]\nPrivateKey=abc=\n  ListenPort =  1 # port\nweird line\n"


@pytest.mark.parametrize(
    "text",
    [MESSY, "[Interface]\nPrivateKey = x", "", "\n\n", "[Interface]\r\nAddress = 10.0.0.1/24\r\n"],
)
def test_round_trip_keeps_text(text: str) -> None:
    assert WgConfig.parse(text).serialize() == text


def test_reads_entries_case_insensitively_and_strips_comments() -> None:
    interface = WgConfig.parse(MESSY).sections_named("interface")[0]

    assert interface.get("privatekey") == "abc="
    assert interface.get("ListenPort") == "1"
    assert interface.get("Missing") is None


def test_set_replaces_existing_entry_in_place() -> None:
    config = WgConfig.parse(MESSY)
    config.sections[0].set("ListenPort", "2")

    assert config.serialize() == MESSY.replace("  ListenPort =  1 # port", "ListenPort = 2")


def test_set_adds_entry_after_last_one() -> None:
    config = WgConfig.parse("[Interface]\nA = 1\n\n# tail\n")
    config.sections[0].set("B", "2")

    assert config.serialize() == "[Interface]\nA = 1\nB = 2\n\n# tail\n"


def test_set_on_empty_section_goes_right_after_header() -> None:
    config = WgConfig.parse("[Interface]\n")
    config.sections[0].set("A", "1")

    assert config.serialize() == "[Interface]\nA = 1\n"


def test_remove_drops_every_line_with_key() -> None:
    config = WgConfig.parse("[Peer]\nAllowedIPs = a\nPublicKey = k\nallowedips = b\n")
    config.sections[0].remove("AllowedIPs")

    assert config.serialize() == "[Peer]\nPublicKey = k\n"


def test_append_starts_section_on_new_line() -> None:
    config = WgConfig.parse("[Interface]\nA = 1")
    config.append(Section.build("Peer", [("PublicKey", "k")]))

    assert config.serialize() == "[Interface]\nA = 1\n[Peer]\nPublicKey = k\n\n"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PublicKey", "k\nAllowedIPs = 0.0.0.0/0"),
        ("PublicKey", "k\r"),
        ("Key", "a # b"),
        ("A B", "1"),
    ],
)
def test_rejects_entries_that_would_inject_lines(key: str, value: str) -> None:
    section = WgConfig.parse("[Peer]\n").sections[0]

    with pytest.raises(ConfigFormatError):
        section.set(key, value)
    with pytest.raises(ConfigFormatError):
        Section.build("Peer", [(key, value)])


def test_section_requires_header() -> None:
    with pytest.raises(ConfigFormatError):
        Section(["A = 1"])
