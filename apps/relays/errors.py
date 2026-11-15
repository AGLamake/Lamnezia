"""Errors of installing, updating and removing a relay, with a message and a hint."""

from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from apps.ssh.errors import CatalogError, ErrorDescription


class RelayErrorCode(StrEnum):
    PORT_RESERVED = "port_reserved"
    PORT_BUSY = "port_busy"
    PORT_AWG = "port_awg"
    MEMBER_ON_RELAY = "member_on_relay"
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
        _("Another program or container listens on it, see the step log. Free the port."),
    ),
    RelayErrorCode.PORT_AWG: ErrorDescription(
        _("The port of a group is used by AmneziaWG on the relay server."),
        _(
            "If this AmneziaWG is a member of the group, pick a relay on another server: every "
            "member listens on the port of the group. Otherwise change its port in the protocol "
            "settings of the server."
        ),
    ),
    RelayErrorCode.MEMBER_ON_RELAY: ErrorDescription(
        _("A member of a group runs on the relay server."),
        _(
            "The relay publishes the port of the group, which every member listens on, so they "
            "cannot share a machine. Pick a relay on another server for the group, or let the "
            "member on this server leave the group."
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
