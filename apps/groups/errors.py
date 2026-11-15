"""Errors of server groups: creating, joining, leaving, handing over; a message and a hint."""

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
    NOT_UP_TO_DATE = "not_up_to_date"
    DIVERGED = "diverged"
    HANDOVER_RUNNING = "handover_running"
    KEYS_CHANGING = "keys_changing"
    AWG_ON_SERVER = "awg_on_server"
    PORT_TAKEN = "port_taken"
    RELAY_SERVER = "relay_server"
    JOIN_RUNNING = "join_running"
    SETTINGS_RUNNING = "settings_running"
    NO_MEMBER_ANSWERS = "no_member_answers"
    NO_LEADER = "no_leader"
    NOT_LEAVING = "not_leaving"
    LEAVE_RUNNING = "leave_running"


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
        _("Make another server of the group the leading one first, then let this one leave."),
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
    GroupErrorCode.NOT_UP_TO_DATE: ErrorDescription(
        _("Only a joined server that has every key change of the group can lead it."),
        _("Wait until the server joins or catches up with the group, then try again."),
    ),
    GroupErrorCode.DIVERGED: ErrorDescription(
        _("The server differs from the group: it cannot lead it."),
        _("Wait for the catch-up or let the server leave and join again, then try again."),
    ),
    GroupErrorCode.HANDOVER_RUNNING: ErrorDescription(
        _("The group is already changing its leading server."),
        _("Wait until that operation finishes."),
    ),
    GroupErrorCode.KEYS_CHANGING: ErrorDescription(
        _("Keys of the group are being changed now."),
        _("Wait until the key operations finish, then try again."),
    ),
    GroupErrorCode.AWG_ON_SERVER: ErrorDescription(
        _("The server already has AmneziaWG of its own."),
        _("Join it as a standalone AmneziaWG 3.1 server, or remove its AmneziaWG first."),
    ),
    GroupErrorCode.PORT_TAKEN: ErrorDescription(
        _("The port of the group is taken on the server."),
        _("Free the UDP port of the group on the server, then join."),
    ),
    GroupErrorCode.RELAY_SERVER: ErrorDescription(
        _("The server runs a relay."),
        _("Install AmneziaWG of the group on a server without a relay."),
    ),
    GroupErrorCode.JOIN_RUNNING: ErrorDescription(
        _("The server is already joining a group."),
        _("Wait until that operation finishes."),
    ),
    GroupErrorCode.SETTINGS_RUNNING: ErrorDescription(
        _("The group is already changing its settings or its leading server."),
        _("Wait until that operation finishes."),
    ),
    GroupErrorCode.NO_MEMBER_ANSWERS: ErrorDescription(
        _("No server of the group answers."),
        _("Nothing is changed. Check that the servers of the group are online, then try again."),
    ),
    GroupErrorCode.NO_LEADER: ErrorDescription(
        _("The group has no leading server."),
        _("Make a server of the group the leading one first."),
    ),
    GroupErrorCode.NOT_LEAVING: ErrorDescription(
        _("The server is not leaving the group."),
        _("Reload the page: another action has already changed it."),
    ),
    GroupErrorCode.LEAVE_RUNNING: ErrorDescription(
        _("The server is leaving the group now."),
        _("Wait until that operation finishes."),
    ),
}


class GroupError(CatalogError[GroupErrorCode]):
    """A server group change is not possible or failed."""

    catalog = GROUP_ERRORS
