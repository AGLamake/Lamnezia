from apps.traffic.services.xray_shaping import XraySpeedRule, speed_marks, speed_nft

EMAIL = "a" * 64
OTHER = "b" * 64


def test_marks_follow_the_emails_not_the_speeds() -> None:
    marks = speed_marks([XraySpeedRule(OTHER, 5), XraySpeedRule(EMAIL, 10)])
    faster = speed_marks([XraySpeedRule(OTHER, 50), XraySpeedRule(EMAIL, 100)])

    assert list(marks.values()) == [1001, 1002]
    assert [rule.email for rule in marks] == [EMAIL, OTHER]
    assert list(faster.values()) == list(marks.values())


def test_nft_polices_each_mark_both_ways() -> None:
    nft = speed_nft([XraySpeedRule(EMAIL, 1), XraySpeedRule(OTHER, 100)])

    assert nft.splitlines()[2:4] == ["table inet lamnezia", "delete table inet lamnezia"]
    assert "meta mark 1001 ct mark set meta mark" in nft
    assert "meta mark 1001 limit rate over 125 kbytes/second burst 128 kbytes drop" in nft
    assert "ct mark 1001 limit rate over 125 kbytes/second burst 128 kbytes drop" in nft
    assert "ct mark 1002 limit rate over 12500 kbytes/second burst 1250 kbytes drop" in nft
    assert "hook output priority mangle" in nft
    assert "hook input priority filter" in nft


def test_nft_without_rules_only_drops_the_table() -> None:
    nft = speed_nft([])

    assert nft.splitlines()[2:] == ["table inet lamnezia", "delete table inet lamnezia"]


def test_rule_round_trips_through_json() -> None:
    rule = XraySpeedRule(EMAIL, 10)

    assert XraySpeedRule.from_json(rule.to_json()) == rule
