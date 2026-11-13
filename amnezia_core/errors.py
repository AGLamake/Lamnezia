class AmneziaFormatError(ValueError):
    """Base error for data that does not match the Amnezia server formats."""


class ConfigFormatError(AmneziaFormatError):
    """awg0.conf cannot be used: missing [Interface], broken Address and similar."""


class ClientsTableError(AmneziaFormatError):
    """clientsTable is not valid JSON or has an unexpected structure."""


class DumpFormatError(AmneziaFormatError):
    """Output of `awg show all dump` has an unexpected shape."""


class InvalidKeyError(AmneziaFormatError):
    """A WireGuard key is not 32 bytes of base64."""


class SubnetExhaustedError(AmneziaFormatError):
    """No free client address is left in the server subnet."""


class PeerConflictError(AmneziaFormatError):
    """A peer cannot be put back: its tunnel address now belongs to another peer."""


class UnsupportedSetupError(AmneziaFormatError):
    """The server uses a protocol setup the panel does not support, e.g. XRay over xhttp."""


class ProtectedClientError(AmneziaFormatError):
    """The client belongs to the Amnezia app that installed the server and must stay."""


class VpnLinkError(AmneziaFormatError):
    """A vpn:// link or QR payload cannot be decoded."""


class ReleaseFormatError(AmneziaFormatError):
    """A GitHub release response of amnezia-client has an unexpected shape."""
