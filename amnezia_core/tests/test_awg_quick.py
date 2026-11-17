from amnezia_core.awg_quick import UpOutput, UpRefusal

KEY = "c2VjcmV0LWhlYWRlci1wcm90ZWN0aW9uLWtleS0wMDA="


def test_unknown_parameter_keeps_only_its_name() -> None:
    output = UpOutput.parse(
        "[#] awg setconf awg0 /dev/fd/63\n"
        f"Line unrecognized: `HeaderProtectionKey={KEY}'\n"
        "Configuration parsing error\n"
        "[#] ip link delete dev awg0\n"
    )

    assert output.refusal is UpRefusal.UNKNOWN_PARAMETER
    assert output.lines == (
        "[#] awg setconf awg0 /dev/fd/63",
        "Line unrecognized: `HeaderProtectionKey=…'",
        "Configuration parsing error",
        "[#] ip link delete dev awg0",
    )
    assert KEY not in "\n".join(output.lines)


def test_unknown_line_cut_short_hides_its_value() -> None:
    output = UpOutput.parse(f"Line unrecognized: `HeaderProtectionKey = {KEY}")

    assert output.lines == ("Line unrecognized: `HeaderProtectionKey=…'",)


def test_invalid_argument() -> None:
    output = UpOutput.parse(
        "[#] awg setconf awg0 /dev/fd/63\nUnable to modify interface: Invalid argument\n"
    )

    assert output.refusal is UpRefusal.INVALID_ARGUMENT


def test_other_output_is_kept() -> None:
    output = UpOutput.parse("\nRTNETLINK answers: Operation not permitted\n\n")

    assert output.refusal is UpRefusal.OTHER
    assert output.lines == ("RTNETLINK answers: Operation not permitted",)
