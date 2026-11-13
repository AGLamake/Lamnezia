"""Errors of peer changes on a server, with a human message and a hint on what to do."""

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from amnezia_core.errors import (
    AmneziaFormatError,
    PeerConflictError,
    ProtectedClientError,
    SubnetExhaustedError,
    UnsupportedSetupError,
)
from apps.ssh.errors import CatalogError, ErrorDescription


class PeerErrorCode(StrEnum):
    SERVER_FILES_INVALID = "server_files_invalid"
    SUBNET_FULL = "subnet_full"
    ADDRESS_TAKEN = "address_taken"
    NOT_APPLIED = "peers_not_applied"
    UNSUPPORTED_SETUP = "unsupported_setup"
    PROTECTED_CLIENT = "protected_client"
    CONFIG_REJECTED = "config_rejected"
    NOT_SERVING = "not_serving"
    DRIFT_CHANGED = "drift_changed"
    CERTIFICATE_GONE = "certificate_gone"
    CERTIFICATE_REJECTED = "certificate_rejected"
    OPENVPN_DOWN = "openvpn_down"
    IPSEC_REJECTED = "ipsec_rejected"
    NOT_ALL_REISSUED = "not_all_reissued"


ERROR_CATALOG: dict[PeerErrorCode, ErrorDescription] = {
    PeerErrorCode.SERVER_FILES_INVALID: ErrorDescription(
        _("The protocol configuration on the server cannot be read."),
        _("The files may have been edited by hand. Check awg0.conf and clientsTable."),
    ),
    PeerErrorCode.SUBNET_FULL: ErrorDescription(
        _("There are no free addresses left in the server subnet."),
        _("Revoke unused keys or move clients to another server."),
    ),
    PeerErrorCode.ADDRESS_TAKEN: ErrorDescription(
        _("The key cannot be turned back on: its address now belongs to another client."),
        _("A client was probably added in the Amnezia app. Revoke this key and issue a new one."),
    ),
    PeerErrorCode.NOT_APPLIED: ErrorDescription(
        _("The server did not apply the new client list."),
        _("Run the operation again. If it repeats, restart the protocol container."),
    ),
    PeerErrorCode.UNSUPPORTED_SETUP: ErrorDescription(
        _("The panel does not support this protocol setup."),
        _("For XRay the panel supports VLESS Reality over TCP, the Amnezia app default."),
    ),
    PeerErrorCode.PROTECTED_CLIENT: ErrorDescription(
        _("This client belongs to the Amnezia app that installed the server."),
        _("Removing it would lock the server owner out. Manage it in the Amnezia app."),
    ),
    PeerErrorCode.CONFIG_REJECTED: ErrorDescription(
        _("XRay rejected the new configuration; the server was not changed."),
        _("The configuration on the server may have been edited by hand. Check server.json."),
    ),
    PeerErrorCode.NOT_SERVING: ErrorDescription(
        _("XRay did not start with the new configuration."),
        _("The previous configuration was put back. Check the container log and try again."),
    ),
    PeerErrorCode.DRIFT_CHANGED: ErrorDescription(
        _("The difference with the server has changed since it was found."),
        _("Check the server again and choose an action for what it shows now."),
    ),
    PeerErrorCode.CERTIFICATE_GONE: ErrorDescription(
        _("The client certificate is no longer on the server."),
        _("It was probably revoked in the Amnezia app. Revoke this key and issue a new one."),
    ),
    PeerErrorCode.CERTIFICATE_REJECTED: ErrorDescription(
        _("The OpenVPN certificate authority refused the change."),
        _("The PKI in the container may have been edited by hand. Check the operation log."),
    ),
    PeerErrorCode.OPENVPN_DOWN: ErrorDescription(
        _("OpenVPN did not start again after the revocation list changed."),
        _("Restart the protocol container and run the operation again."),
    ),
    PeerErrorCode.IPSEC_REJECTED: ErrorDescription(
        _("The certificate database of IPsec refused the change."),
        _("The database in the container may have been edited by hand. Check the operation log."),
    ),
    PeerErrorCode.NOT_ALL_REISSUED: ErrorDescription(
        _("Some keys were not reissued; they keep their old keys."),
        _("The step log names them and why. Run the operation again: it retries only them."),
    ),
}


class PeerError(CatalogError[PeerErrorCode]):
    """A peer change failed."""

    catalog = ERROR_CATALOG


@contextmanager
def explained_format_errors() -> Iterator[None]:
    """Turn amnezia_core errors about server files into a PeerError people can act on."""
    try:
        yield
    except PeerConflictError as error:
        raise PeerError(PeerErrorCode.ADDRESS_TAKEN, str(error)) from error
    except SubnetExhaustedError as error:
        raise PeerError(PeerErrorCode.SUBNET_FULL, str(error)) from error
    except UnsupportedSetupError as error:
        raise PeerError(PeerErrorCode.UNSUPPORTED_SETUP, str(error)) from error
    except ProtectedClientError as error:
        raise PeerError(PeerErrorCode.PROTECTED_CLIENT, str(error)) from error
    except AmneziaFormatError as error:
        raise PeerError(PeerErrorCode.SERVER_FILES_INVALID, str(error)) from error
