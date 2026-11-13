"""`$VARIABLE` placeholders of upstream server scripts and templates (amnezia::genAwgVars)."""

import re
from collections.abc import Mapping

# Template variable -> awg0.conf parameter, as in upstream genAwgVars.
PARAM_VARIABLES = {
    "$JUNK_PACKET_COUNT": "Jc",
    "$JUNK_PACKET_MIN_SIZE": "Jmin",
    "$JUNK_PACKET_MAX_SIZE": "Jmax",
    "$INIT_PACKET_JUNK_SIZE": "S1",
    "$RESPONSE_PACKET_JUNK_SIZE": "S2",
    "$COOKIE_REPLY_PACKET_JUNK_SIZE": "S3",
    "$TRANSPORT_PACKET_JUNK_SIZE": "S4",
    "$INIT_PACKET_MAGIC_HEADER": "H1",
    "$RESPONSE_PACKET_MAGIC_HEADER": "H2",
    "$UNDERLOAD_PACKET_MAGIC_HEADER": "H3",
    "$TRANSPORT_PACKET_MAGIC_HEADER": "H4",
    "$SPECIAL_JUNK_1": "I1",
    "$SPECIAL_JUNK_2": "I2",
    "$SPECIAL_JUNK_3": "I3",
    "$SPECIAL_JUNK_4": "I4",
    "$SPECIAL_JUNK_5": "I5",
    "$HEADER_PROTECTION_KEY": "HeaderProtectionKey",
    "$CONTENT_PADDING_ADDITION": "ContentPaddingAddition",
    "$REKEY_AFTER_TIME": "RekeyAfterTime",
    "$REKEY_TIMEOUT": "RekeyTimeout",
    "$REJECT_AFTER_TIME": "RejectAfterTime",
    "$KEEPALIVE_TIMEOUT": "KeepaliveTimeout",
    "$MAX_HANDSHAKE_ATTEMPTS": "MaxHandshakeAttempts",
    "$RANDOM_TRAILERS": "RandomTrailers",
    "$DISABLE_COOKIES": "DisableCookies",
}
TOGGLES = ("RandomTrailers", "DisableCookies")

_VARIABLE = re.compile(r"\$[A-Z0-9_]+")


def container_variables(container: str) -> dict[str, str]:
    """`$CONTAINER_NAME` and `$DOCKERFILE_FOLDER` of every container script (genBaseVars)."""
    return {"$CONTAINER_NAME": container, "$DOCKERFILE_FOLDER": f"/opt/amnezia/{container}"}


def is_toggle_enabled(value: str) -> bool:
    return bool(value.strip()) and value.strip().lower() != "off"


def substitute(template: str, variables: Mapping[str, str]) -> str:
    """Replace known `$NAME` placeholders; unknown ones (shell variables) stay as they are."""
    return _VARIABLE.sub(lambda match: variables.get(match[0], match[0]), template)
