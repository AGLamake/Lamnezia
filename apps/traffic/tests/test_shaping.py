from ipaddress import IPv4Address

from amnezia_core.containers import AWG2
from apps.traffic.services.shaping import (
    SpeedRule,
    TotalSpeed,
    needs_shaping,
    speed_script,
    speed_script_path,
    tries_ifb,
)

PHONE = SpeedRule(IPv4Address("10.8.1.2"), 10)
LAPTOP = SpeedRule(IPv4Address("10.8.1.3"), 1)


def commands(script: str) -> list[str]:
    return [line for line in script.splitlines() if line.startswith("tc ")]


def test_script_without_rules_only_removes_the_old_ones() -> None:
    script = speed_script("awg0", [])

    assert commands(script) == [
        'tc qdisc del dev "$dev" root 2>/dev/null || true',
        'tc qdisc del dev "$dev" ingress 2>/dev/null || true',
    ]
    assert 'ip link del "$ifb" 2>/dev/null || true' in script.splitlines()
    assert speed_script("awg0", [], ifb=True) == script


def test_each_rule_limits_download_and_polices_upload_without_ifb() -> None:
    script = speed_script("awg0", [LAPTOP, PHONE])

    assert "dev=awg0" in script.splitlines()
    assert commands(script)[2:] == [
        'tc qdisc add dev "$dev" root handle 1: htb',
        'tc class add dev "$dev" parent 1: classid 1:1 htb rate 10mbit ceil 10mbit',
        'tc filter add dev "$dev" parent 1: protocol ip prio 1 u32'
        " match ip dst 10.8.1.2/32 flowid 1:1",
        'tc class add dev "$dev" parent 1: classid 1:2 htb rate 1mbit ceil 1mbit',
        'tc filter add dev "$dev" parent 1: protocol ip prio 1 u32'
        " match ip dst 10.8.1.3/32 flowid 1:2",
        'tc qdisc add dev "$dev" handle ffff: ingress',
        'tc filter add dev "$dev" parent ffff: protocol ip prio 1 u32'
        " match ip src 10.8.1.2/32 police rate 10mbit burst 131072 mtu 65536 drop flowid :1",
        'tc filter add dev "$dev" parent ffff: protocol ip prio 1 u32'
        " match ip src 10.8.1.3/32 police rate 1mbit burst 131072 mtu 65536 drop flowid :1",
    ]


def test_with_ifb_upload_is_shaped_by_the_same_classes() -> None:
    lines = speed_script("awg0", [PHONE], ifb=True).splitlines()

    assert "ifb=ifb_awg0" in lines
    upload = lines[lines.index('ip link add "$ifb" type ifb') :]
    assert upload == [
        'ip link add "$ifb" type ifb',
        'ip link set "$ifb" up',
        'tc qdisc add dev "$dev" handle ffff: ingress',
        'tc filter add dev "$dev" parent ffff: protocol all prio 1 u32'
        ' match u32 0 0 action mirred egress redirect dev "$ifb"',
        'tc qdisc add dev "$ifb" root handle 1: htb',
        'tc class add dev "$ifb" parent 1: classid 1:1 htb rate 10mbit ceil 10mbit',
        'tc filter add dev "$ifb" parent 1: protocol ip prio 1 u32'
        " match ip src 10.8.1.2/32 flowid 1:1",
    ]
    assert not any("police" in line for line in lines)


def test_a_script_for_an_unknown_host_tries_ifb_and_polices_without_it() -> None:
    script = speed_script("lg51820", [PHONE], ifb=None)
    lines = script.splitlines()

    start = lines.index('if ip link add "$ifb" type ifb 2>/dev/null; then')
    shaped, policed = lines[start + 1 : lines.index("else")], lines[lines.index("else") + 1 :]
    assert shaped[0] == '  ip link set "$ifb" up'
    assert '  tc qdisc add dev "$ifb" root handle 1: htb' in shaped
    assert policed[-1] == "fi" and "police rate 10mbit" in policed[-2]
    assert tries_ifb(script)


def test_an_upload_total_without_ifb_is_left_unshaped() -> None:
    lines = speed_script("lg51820", [], TotalSpeed(upload=20), ifb=None).splitlines()

    assert lines[lines.index("else") + 1 :] == ["  :", "fi"]


def test_a_script_without_upload_limits_does_not_try_ifb() -> None:
    script = speed_script("lg51820", [], TotalSpeed(download=20), ifb=None)

    assert not tries_ifb(script)
    assert not tries_ifb(speed_script("awg0", [PHONE], ifb=True))


def test_total_limit_shares_equally_between_all_peers() -> None:
    """Equal rates and quanta under the parent: those who download now split the total evenly."""
    free = SpeedRule(IPv4Address("10.8.1.4"))
    script = speed_script("awg0", [PHONE, free], TotalSpeed(download=30))

    assert commands(script)[2:] == [
        'tc qdisc add dev "$dev" root handle 1: htb default ffff',
        'tc class add dev "$dev" parent 1: classid 1:1 htb rate 30mbit ceil 30mbit',
        'tc class add dev "$dev" parent 1:1 classid 1:2 htb rate 10000kbit ceil 10mbit'
        " quantum 1514",
        'tc filter add dev "$dev" parent 1: protocol ip prio 1 u32'
        " match ip dst 10.8.1.2/32 flowid 1:2",
        'tc class add dev "$dev" parent 1:1 classid 1:3 htb rate 10000kbit ceil 30mbit'
        " quantum 1514",
        'tc filter add dev "$dev" parent 1: protocol ip prio 1 u32'
        " match ip dst 10.8.1.4/32 flowid 1:3",
        'tc class add dev "$dev" parent 1:1 classid 1:ffff htb rate 10000kbit ceil 30mbit'
        " quantum 1514",
        # Upload has no total, so only the personal limit is policed.
        'tc qdisc add dev "$dev" handle ffff: ingress',
        'tc filter add dev "$dev" parent ffff: protocol ip prio 1 u32'
        " match ip src 10.8.1.2/32 police rate 10mbit burst 131072 mtu 65536 drop flowid :1",
    ]


def test_children_of_the_total_never_add_up_over_it() -> None:
    rules = [SpeedRule(IPv4Address(f"10.8.{n // 250}.{n % 250 + 2}")) for n in range(999)]
    script = speed_script("awg0", rules, TotalSpeed(download=10, upload=10), ifb=True)

    rates = [
        int(line.split(" rate ")[1].split("kbit")[0])
        for line in script.splitlines()
        if "parent 1:1 " in line and 'dev "$dev"' in line
    ]
    assert len(rates) == 1000
    assert sum(rates) <= 10_000
    assert 'dev "$ifb" parent 1:1 classid 1:3e8 ' in script


def test_personal_limit_over_the_total_is_capped_by_it() -> None:
    script = speed_script("awg0", [SpeedRule(IPv4Address("10.8.1.2"), 500)], TotalSpeed(100))

    assert "classid 1:2 htb rate 50000kbit ceil 100mbit quantum 1514" in script


def test_upload_total_alone_leaves_download_unshaped() -> None:
    script = speed_script(
        "awg0", [SpeedRule(IPv4Address("10.8.1.2"))], TotalSpeed(upload=5), ifb=True
    )

    assert 'tc qdisc add dev "$dev" root' not in script
    assert 'tc class add dev "$ifb" parent 1: classid 1:1 htb rate 5mbit ceil 5mbit' in script


def test_nothing_is_shaped_without_limits() -> None:
    assert not needs_shaping([SpeedRule(IPv4Address("10.8.1.2"))], TotalSpeed())
    assert needs_shaping([PHONE], TotalSpeed())
    assert needs_shaping([], TotalSpeed(upload=1))


def test_total_survives_json() -> None:
    assert TotalSpeed.from_json(TotalSpeed(100, None).to_json()) == TotalSpeed(100, None)
    assert TotalSpeed.from_json(None) == TotalSpeed()
    assert not TotalSpeed()


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
    assert SpeedRule.from_json({"address": "10.8.1.4", "mbit": None}).mbit is None
    assert PHONE.to_json() == {"address": "10.8.1.2", "mbit": 10}


def test_script_lives_next_to_the_protocol_config() -> None:
    assert speed_script_path(AWG2) == "/opt/amnezia/awg/lamnezia_speed.sh"
