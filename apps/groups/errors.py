"""Errors of creating a server group, joining and leaving it, with a message and a hint."""

from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from apps.ssh.errors import CatalogError, ErrorDescription


class GroupErrorCode(StrEnum):
    NOT_AWG3 = "not_awg3"
    IN_GROUP = "in_group"
    CONTAINER_REMOVED = "container_removed"
    OTHER_PORT = "other_port"
    OTHER_SUBNET = "other_subnet"
    LEADER_LEAVES = "leader_leaves"
    MEMBERS_LEFT = "members_left"
    NO_IDENTITY = "no_identity"
    OTHER_SERVER_KEY = "other_server_key"
    NOT_CLONE = "not_clone"


GROUP_ERRORS: dict[GroupErrorCode, ErrorDescription] = {
    GroupErrorCode.NOT_AWG3: ErrorDescription(
        _("Only AmneziaWG 3.1 servers can be in a group."),
        _("Move the AmneziaWG 2.0 server to 3.1 on its card first."),
    ),
    GroupErrorCode.IN_GROUP: ErrorDescription(
        _("The server is already in a group."),
        _("A server is either standalone or a member of one group. Let it leave first."),
    ),
    GroupErrorCode.CONTAINER_REMOVED: ErrorDescription(
        _("The protocol container is removed from the server."),
        _("Restore the protocol on the server card first."),
    ),
    GroupErrorCode.OTHER_PORT: ErrorDescription(
        _("The server listens on another port than the group."),
        _("Set the port of the group in the protocol settings of the server, then join."),
    ),
    GroupErrorCode.OTHER_SUBNET: ErrorDescription(
        _("The server has another client subnet than the group."),
        _("Set the subnet of the group in the protocol settings of the server, then join."),
    ),
    GroupErrorCode.LEADER_LEAVES: ErrorDescription(
        _("The leading server cannot leave while other servers are in the group."),
        _("Let the other servers leave first, then delete the group."),
    ),
    GroupErrorCode.MEMBERS_LEFT: ErrorDescription(
        _("The group still has other servers."),
        _("Let them leave first: then the leading server stays as a standalone one."),
    ),
    GroupErrorCode.NO_IDENTITY: ErrorDescription(
        _("No server of the group could be read."),
        _("The panel copies the files of a member that answers. Check that one is online."),
    ),
    GroupErrorCode.OTHER_SERVER_KEY: ErrorDescription(
        _("The server runs AmneziaWG with other keys than the panel has."),
        _("Import the protocol from the server again, then join."),
    ),
    GroupErrorCode.NOT_CLONE: ErrorDescription(
        _("After writing the files the server still differs from the group."),
        _("Run the operation again. If it repeats, report a bug."),
    ),
}


class GroupError(CatalogError[GroupErrorCode]):
    """A server group change is not possible or failed."""

    catalog = GROUP_ERRORS
