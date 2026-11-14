"""Errors of installing, updating and removing a relay, with a message and a hint."""

from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from apps.ssh.errors import CatalogError, ErrorDescription


class RelayErrorCode(StrEnum):
    PORT_RESERVED = "port_reserved"
    PORT_BUSY = "port_busy"
    NOT_INSTALLED = "not_installed"
    NOT_REMOVED = "not_removed"
    NO_MEMBER = "no_member"
    HOST_NOT_RESOLVED = "host_not_resolved"


RELAY_ERRORS: dict[RelayErrorCode, ErrorDescription] = {
    RelayErrorCode.PORT_RESERVED: ErrorDescription(
        _("The port of a group is one the web server of the panel needs."),
        _(
            "Ports 80 and 443 belong to the panel's web server (HTTPS and HTTP/3) on its machine. "
            "Give the group another port."
        ),
    ),
    RelayErrorCode.PORT_BUSY: ErrorDescription(
        _("The port of a group is already used on the relay server."),
        _(
            "Another program or container listens on it, see the step log. A member of the "
            "group cannot be on the relay server with the same port: pick another relay."
        ),
    ),
    RelayErrorCode.NOT_INSTALLED: ErrorDescription(
        _("The relay container is not on the server."),
        _("Install the relay on the server card again."),
    ),
    RelayErrorCode.NOT_REMOVED: ErrorDescription(
        _("The relay container is still on the server."),
        _("See the step log; remove it with `docker rm -f lamnezia-relay` and try again."),
    ),
    RelayErrorCode.NO_MEMBER: ErrorDescription(
        _("No member of the group could be reached to add the service peer."),
        _("Check that the members are online; the relay is updated again on the next sync."),
    ),
    RelayErrorCode.HOST_NOT_RESOLVED: ErrorDescription(
        _("The address of a member server could not be found."),
        _("The relay forwards to an IPv4 address: check the DNS name of the server."),
    ),
}


class RelayError(CatalogError[RelayErrorCode]):
    """A relay cannot be installed, updated or removed."""

    catalog = RELAY_ERRORS
