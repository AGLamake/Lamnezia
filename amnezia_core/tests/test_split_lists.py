"""Reading the sources of the split network list, own entries and the merge."""

import json
from ipaddress import IPv4Network

import pytest

from amnezia_core.relay import DomainList
from amnezia_core.split_lists import (
    SourceFormat,
    SplitListError,
    Target,
    direct_networks,
)


def nets(*values: str) -> tuple[IPv4Network, ...]:
    return tuple(IPv4Network(value) for value in values)


class TestCidr:
    def test_merges_and_skips_comments_and_ipv6(self) -> None:
        text = "# ru\n5.9.0.0/16\n5.8.0.0/16  # merged\n\n2a00::/16\n77.88.8.8\n"

        assert SourceFormat.CIDR.parse(text) == nets("5.8.0.0/15", "77.88.8.8/32")

    def test_host_bits_are_dropped(self) -> None:
        assert SourceFormat.CIDR.parse("5.8.1.7/16\n") == nets("5.8.0.0/16")

    def test_a_bad_line_breaks_the_list(self) -> None:
        with pytest.raises(SplitListError, match="Line 2"):
            SourceFormat.CIDR.parse("5.8.0.0/16\n<html>\n")

    @pytest.mark.parametrize("text", ["", "# only\n", "2a00::/16\n"])
    def test_a_list_without_ipv4_is_refused(self, text: str) -> None:
        with pytest.raises(SplitListError, match="no IPv4"):
            SourceFormat.CIDR.parse(text)


class TestLib4u:
    def test_reads_hostnames_and_addresses_as_the_application_does(self) -> None:
        sites = [
            {"hostname": "2.16.56.69/32", "ip": ""},
            {
                "hostname": "ya.ru",
                "ips": ["77.88.44.242", "2a02:6b8::2:242"],
                "ip": "5.255.255.242",
            },
            {"hostname": "vk.com", "ip": "87.240.132.72"},
            {"hostname": "1.0.0.in-addr.arpa", "ips": [], "ip": ""},
            {"hostname": "example.ru"},
            "not an object",
            {"hostname": 7, "ips": "77.88.55.80"},
        ]

        assert SourceFormat.LIB4U.parse(json.dumps(sites)) == nets(
            "2.16.56.69/32", "5.255.255.242/32", "77.88.44.242/32", "87.240.132.72/32"
        )

    @pytest.mark.parametrize("text", ["{oops", '{"hostname": "1.2.3.4"}'])
    def test_not_an_array_is_refused(self, text: str) -> None:
        with pytest.raises(SplitListError):
            SourceFormat.LIB4U.parse(text)

    def test_only_domains_without_addresses_is_empty(self) -> None:
        with pytest.raises(SplitListError, match="no IPv4"):
            SourceFormat.LIB4U.parse('[{"hostname": "ya.ru"}]')


RIPE = """2|ripencc|1763593199|236218|19830705|20251119|+0100
ripencc|*|ipv4|*|90543|summary
ripencc|RU|ipv4|2.56.24.0|512|20190313|allocated
ripencc|RU|ipv4|2.56.26.0|256|20190313|allocated
ripencc|RU|ipv4|5.8.0.0|768|20120117|assigned|extra
ripencc|DE|ipv4|2.56.27.0|256|20190313|allocated
ripencc|RU|ipv4|2.60.0.0|65536|20100101|reserved
ripencc|RU|ipv6|2a00:1fa0::|29|20090316|allocated
ripencc|RU|asn|8359|1|20000101|allocated
"""


class TestRipe:
    def test_reads_the_blocks_of_the_country(self) -> None:
        assert SourceFormat.RIPE.parse(RIPE) == nets(
            "2.56.24.0/23", "2.56.26.0/24", "5.8.0.0/23", "5.8.2.0/24"
        )

    def test_a_bad_block_breaks_the_list(self) -> None:
        with pytest.raises(SplitListError, match="Line 2"):
            SourceFormat.RIPE.parse("x\nripencc|RU|ipv4|5.8.0.0|many|20120117|assigned\n")

    def test_a_page_instead_of_the_file_is_empty(self) -> None:
        with pytest.raises(SplitListError, match="no IPv4"):
            SourceFormat.RIPE.parse("<html><body>Not found</body></html>")


class TestTarget:
    @pytest.mark.parametrize(
        ("value", "network", "domain"),
        [
            ("5.8.0.0/16", "5.8.0.0/16", ""),
            (" 77.88.8.8 ", "77.88.8.8/32", ""),
            ("Ya.RU.", None, "ya.ru"),
            ("пример.рф", None, "xn--e1afmkfd.xn--p1ai"),
        ],
    )
    def test_parses_a_network_or_a_domain(
        self, value: str, network: str | None, domain: str
    ) -> None:
        target = Target.parse(value)

        assert target.network == (IPv4Network(network) if network else None)
        assert target.domain == domain

    @pytest.mark.parametrize(
        "value", ["", "localhost", "*.ya.ru", "a b.ru", "-x.ru", "1.2.3", "2a00::/16", "a..ru"]
    )
    def test_refuses_what_is_neither(self, value: str) -> None:
        with pytest.raises(SplitListError):
            Target.parse(value)


class TestDirectNetworks:
    def test_sources_and_local_entries_without_abroad_ones(self) -> None:
        found = direct_networks(
            nets("5.8.0.0/16", "5.9.0.0/16"), nets("10.10.0.0/24"), nets("5.8.128.0/17")
        )

        assert found == nets("5.8.0.0/17", "5.9.0.0/16", "10.10.0.0/24")

    def test_an_abroad_network_wider_than_a_list_network_drops_it(self) -> None:
        assert direct_networks(nets("5.8.1.0/24", "2.60.0.0/16"), (), nets("5.8.0.0/16")) == (
            nets("2.60.0.0/16")
        )

    def test_everything_abroad_leaves_nothing(self) -> None:
        assert direct_networks(nets("5.8.0.0/16"), (), nets("0.0.0.0/0")) == ()


def test_domain_list_text_names_the_direction_of_each_domain() -> None:
    domains = DomainList(local=("ya.ru",), abroad=("example.com", "x.org"))

    assert domains.to_text() == "local ya.ru\nabroad example.com\nabroad x.org\n"
    assert not DomainList()
