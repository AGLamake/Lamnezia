from amnezia_core.start_script import XRAY_STOP_LINE, with_line_after, with_startup_line

LINE = "sh /opt/amnezia/awg/extra.sh || true"
UPSTREAM = (
    "#!/bin/bash\n"
    "if [ -f /opt/amnezia/awg/awg0.conf ]; then (awg-quick up /opt/amnezia/awg/awg0.conf); fi\n"
    "iptables -A INPUT -i awg0 -j ACCEPT\n"
    "\n"
    "tail -f /dev/null\n"
)


def test_line_goes_before_the_keep_running_command() -> None:
    script = with_startup_line(UPSTREAM, LINE)

    assert script is not None
    assert script.splitlines()[-2:] == [LINE, "tail -f /dev/null"]
    assert script.startswith(UPSTREAM.removesuffix("tail -f /dev/null\n"))


def test_script_with_the_line_stays_the_same() -> None:
    script = with_startup_line(UPSTREAM, LINE)

    assert script is not None
    assert with_startup_line(script, LINE) == script


def test_script_of_a_fresh_container_gets_the_line() -> None:
    # The Dockerfile writes this one before the app uploads the real script.
    assert with_startup_line("#!/bin/bash\ntail -f /dev/null", LINE) == (
        f"#!/bin/bash\n{LINE}\ntail -f /dev/null\n"
    )


def test_script_that_never_waits_has_no_place_for_the_line() -> None:
    assert with_startup_line("#!/bin/sh\nexec xray run\n", LINE) is None


XRAY_START = (
    "#!/bin/bash\n"
    "killall -KILL xray\n"
    "\n"
    "if [ -f /opt/amnezia/xray/server.json ]; then"
    " (xray -config /opt/amnezia/xray/server.json); fi\n"
    "\n"
    "tail -f /dev/null\n"
)


def test_line_goes_right_after_the_anchor() -> None:
    script = with_line_after(XRAY_START, LINE, XRAY_STOP_LINE)

    assert script is not None
    assert script.splitlines()[1:3] == [XRAY_STOP_LINE, LINE]
    assert with_line_after(script, LINE, XRAY_STOP_LINE) == script


def test_script_without_the_anchor_has_no_place_for_the_line() -> None:
    assert with_line_after("#!/bin/sh\nexec xray run\n", LINE, XRAY_STOP_LINE) is None
