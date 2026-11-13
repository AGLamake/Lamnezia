from ipaddress import IPv4Address

from amnezia_core.containers import AWG2
from apps.traffic.services.shaping import SpeedRule, speed_script, speed_script_path

PHONE = SpeedRule(IPv4Address("10.8.1.2"), 10)
LAPTOP = SpeedRule(IPv4Address("10.8.1.3"), 1)


def commands(script: str) -> list[str]:
    return [line for line in script.splitlines() if line.startswith("tc ")]


def test_script_without_rules_only_removes_the_old_ones() -> None:
    assert commands(speed_script("awg0", [])) == [
        'tc qdisc del dev "$dev" root 2>/dev/null || true',
        'tc qdisc del dev "$dev" ingress 2>/dev/null || true',
    ]


def test_each_rule_limits_download_and_upload_of_its_address() -> None:
    script = speed_script("awg0", [LAPTOP, PHONE])

    assert "dev=awg0" in script.splitlines()
    assert commands(script)[2:] == [
        'tc qdisc add dev "$dev" root handle 1: htb',
        'tc qdisc add dev "$dev" handle ffff: ingress',
        'tc class add dev "$dev" parent 1: classid 1:1 htb rate 10mbit ceil 10mbit',
        'tc filter add dev "$dev" parent 1: protocol ip prio 1 u32'
        " match ip dst 10.8.1.2/32 flowid 1:1",
        'tc filter add dev "$dev" parent ffff: protocol ip prio 1 u32'
        " match ip src 10.8.1.2/32 police rate 10mbit burst 131072 mtu 65536 drop flowid :1",
        'tc class add dev "$dev" parent 1: classid 1:2 htb rate 1mbit ceil 1mbit',
        'tc filter add dev "$dev" parent 1: protocol ip prio 1 u32'
        " match ip dst 10.8.1.3/32 flowid 1:2",
        'tc filter add dev "$dev" parent ffff: protocol ip prio 1 u32'
        " match ip src 10.8.1.3/32 police rate 1mbit burst 131072 mtu 65536 drop flowid :1",
    ]


def test_fast_limit_lets_a_tenth_of_a_second_through() -> None:
    assert SpeedRule(IPv4Address("10.8.1.2"), 100).burst == 1_250_000


def test_policing_passes_packets_glued_by_the_tunnel() -> None:
    """amneziawg-go writes TCP segments glued up to 64 KB; a smaller burst or mtu drops them all."""
    script = speed_script("awg0", [LAPTOP])

    assert LAPTOP.burst >= 2 * 65536
    assert " mtu 65536 " in script


def test_class_numbers_are_hexadecimal_as_tc_reads_them() -> None:
    rules = [SpeedRule(IPv4Address(f"10.8.1.{n}"), 5) for n in range(2, 12)]

    assert "classid 1:a htb" in speed_script("awg0", rules)


def test_rule_survives_json() -> None:
    assert SpeedRule.from_json(PHONE.to_json()) == PHONE
    assert PHONE.to_json() == {"address": "10.8.1.2", "mbit": 10}


def test_script_lives_next_to_the_protocol_config() -> None:
    assert speed_script_path(AWG2) == "/opt/amnezia/awg/lamnezia_speed.sh"
