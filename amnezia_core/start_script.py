"""The start script of Amnezia containers, /opt/amnezia/start.sh.

Docker runs it on every container start (upstream server_scripts/<type>/Dockerfile ENTRYPOINT).
It brings the interface up and ends with `tail -f /dev/null`, which keeps the container running,
so a command meant to run at start must come before that line.
"""

START_SCRIPT_PATH = "/opt/amnezia/start.sh"
# Docker runs the script directly, so it must stay executable.
START_SCRIPT_MODE = 0o755
_KEEP_RUNNING = "tail -f /dev/null"
# The XRay start script kills a previous xray here, then runs xray in the foreground.
XRAY_STOP_LINE = "killall -KILL xray"


def with_startup_line(script: str, line: str) -> str | None:
    """The script with `line` run at start, or `None` if there is no place for it.

    The line goes right before `tail -f /dev/null`; a script that has it already stays the same.
    """
    lines = script.splitlines()
    if line in lines:
        return script
    ends = [index for index, text in enumerate(lines) if text.strip() == _KEEP_RUNNING]
    if not ends:
        return None
    lines.insert(ends[-1], line)
    return "\n".join(lines) + "\n"


def with_line_after(script: str, line: str, anchor: str) -> str | None:
    """The script with `line` right after the last `anchor` line, or `None` without one.

    For a script that runs its daemon in the foreground, where nothing after it runs: the XRay
    script stops a previous xray at `XRAY_STOP_LINE` and starts the new one next. A script that
    has `line` already stays the same.
    """
    lines = script.splitlines()
    if line in lines:
        return script
    anchors = [index for index, text in enumerate(lines) if text.strip() == anchor]
    if not anchors:
        return None
    lines.insert(anchors[-1] + 1, line)
    return "\n".join(lines) + "\n"
