"""Staff commands in the bots (DESIGN.md 6.8): a summary of the servers, today's traffic, open
incidents with «Take» and «Close», and answers to users by replying to a support notification.

Only administrators and moderators run them; anyone else hears they are not available.
Closing an incident is for administrators, as on the incidents page. A moderator sees only the
servers of their zone and their incidents.
"""

import re
from collections.abc import Mapping

from django.db.models import QuerySet
from django.template.defaultfilters import filesizeformat
from django.utils import formats, timezone
from django.utils.translation import gettext as _

from apps.accounts.services.zone import Zone
from apps.bots.commands.base import CommandSet, Handler, bullets, nav, number, unavailable
from apps.bots.services.replies import Button, Command, Reply
from apps.dashboard.services.today import traffic_today
from apps.servers.models import Server, ServerSample
from apps.support.models import Incident
from apps.support.services.inbox import Inbox
from apps.support.services.incidents import (
    close_incident,
    incidents,
    servers_with_open_incident,
    take_incident,
    takers,
)
from apps.support.services.tickets import ClosedConversationError

ADMIN = "admin"
# The command a staff member's reply to a support notification becomes.
REPLY = "reply"
# The number of the request or chat in the title of a support notification: «New request #12 …».
_NUMBER = re.compile(r"#(\d+)")
_LISTED = 10


def replied_number(text: str) -> int | None:
    """The request or chat a reply to a support notification answers; None for other messages."""
    match = _NUMBER.search(text)
    return int(match[1]) if match else None


class AdminCommands(CommandSet):
    def handlers(self) -> Mapping[str, Handler]:
        handlers: dict[str, Handler] = {
            ADMIN: self.admin,
            "overview": self.overview,
            "today": self.today,
            "incidents": self.incidents,
            "incident": self.incident,
            "take": self.take,
            "close": self.close,
            REPLY: self.reply,
        }
        if self.user.is_staff_member:
            return handlers
        return dict.fromkeys(handlers, _not_for_users)

    def admin(self, command: Command) -> Reply:
        return Reply(
            _("What would you like to check?"),
            (
                Button(_("Server summary"), "overview"),
                Button(_("Traffic today"), "today"),
                Button(_("Incidents"), "incidents"),
                *nav(),
            ),
            title=_("Administration"),
        )

    def overview(self, command: Command) -> Reply:
        title = _("Server summary")
        servers = list(self._servers().select_related("latest_sample"))
        if not servers:
            return Reply(_("No servers yet."), nav(ADMIN), title=title)
        troubled = servers_with_open_incident()
        lines = [_server_line(server, server.pk in troubled) for server in servers]
        return Reply(
            bullets(lines), (Button(_("Incidents"), "incidents"), *nav(ADMIN)), title=title
        )

    def today(self, command: Command) -> Reply:
        traffic = traffic_today(servers=self._servers())
        lines = [
            _("All servers: %(total)s") % {"total": filesizeformat(traffic.total)},
            *(f"{item.server.name}: {filesizeformat(item.total)}" for item in traffic.servers),
        ]
        return Reply(bullets(lines), nav(ADMIN), title=_("Traffic today"))

    def incidents(self, command: Command) -> Reply:
        title = _("Incidents")
        open_ones = list(self._incidents().filter(closed_at=None)[:_LISTED])
        if not open_ones:
            return Reply(_("No open incidents."), nav(ADMIN), title=title)
        buttons = tuple(Button(i.server.name, f"incident {i.pk}") for i in open_ones)
        return Reply(
            bullets(_incident_line(i) for i in open_ones), (*buttons, *nav(ADMIN)), title=title
        )

    def incident(self, command: Command) -> Reply:
        incident = self._find(command)
        return unavailable() if incident is None else self._card(incident)

    def take(self, command: Command) -> Reply:
        incident = self._find(command)
        if incident is None:
            return unavailable()
        if not incident.is_open:
            return self._card(incident)
        if not take_incident(incident, self.user):
            return self._card(incident, _("None of its requests are from your users."))
        return self._card(incident, _("You took the incident: its requests are yours now."))

    def close(self, command: Command) -> Reply:
        """`close <id>`: ask; `close <id> yes`: close it, the reporters hear the server works."""
        incident = self._find(command)
        if incident is None or not self.user.is_admin:
            return unavailable()
        if not incident.is_open:
            return self._card(incident)
        if command.arg(1) != "yes":
            return Reply(
                _("Close the incident on %(server)s? Its requests close and the users are told.")
                % {"server": incident.server.name},
                (Button(_("Close"), f"close {incident.pk} yes"), *nav(f"incident {incident.pk}")),
                title=_("Incident on %(server)s") % {"server": incident.server.name},
            )
        close_incident(incident, self.user)
        return Reply(
            _("The incident is closed; the users are told."),
            (Button(_("Incidents"), "incidents"), *nav(ADMIN)),
            title=_("Incident on %(server)s") % {"server": incident.server.name},
        )

    def reply(self, command: Command) -> Reply:
        """The typed reply to a support notification goes to that request or chat as an answer."""
        inbox = Inbox(self.user)
        pk = number(command.arg(0))
        conversation = None if pk is None else inbox.visible().filter(pk=pk).first()
        if conversation is None or not command.text:
            return unavailable()
        try:
            inbox.reply(conversation, command.text)
        except ClosedConversationError:
            return Reply(_("This request is closed."), nav())
        inbox.mark_read(conversation)
        return Reply(_("Sent to %(user)s.") % {"user": conversation.user.get_username()}, nav())

    def _find(self, command: Command) -> Incident | None:
        pk = number(command.arg(0))
        return None if pk is None else self._incidents().filter(pk=pk).first()

    def _servers(self) -> QuerySet[Server]:
        return Zone(self.user).narrow(Server.objects.all(), server="pk")

    def _incidents(self) -> QuerySet[Incident]:
        return Zone(self.user).narrow(incidents(), server="server")

    def _card(self, incident: Incident, note: str = "") -> Reply:
        taken = ", ".join(takers(incident))
        lines = [
            _("Opened: %(time)s") % {"time": _time(incident)},
            _("Reports: %(reports)s") % {"reports": getattr(incident, "reports", 0)},
            _("Taken by: %(staff)s") % {"staff": taken} if taken else _("Nobody has taken it."),
        ]
        if not incident.is_open:
            lines.append(_("Closed."))
        buttons: list[Button] = []
        if incident.is_open:
            buttons.append(Button(_("Take"), f"take {incident.pk}"))
            if self.user.is_admin:
                buttons.append(Button(_("Close"), f"close {incident.pk}"))
        text = bullets(lines)
        return Reply(
            f"{note}\n\n{text}" if note else text,
            (*buttons, *nav("incidents")),
            title=_("Incident on %(server)s") % {"server": incident.server.name},
        )


def _not_for_users(command: Command) -> Reply:
    return unavailable()


def _server_line(server: Server, incident: bool) -> str:
    parts = [_state(server)]
    if server.online and server.latest_sample is not None:
        parts.extend(_load(server.latest_sample))
    if server.maintenance:
        parts.append(_("under maintenance"))
    if incident:
        parts.append(_("open incident"))
    return f"{server.name}: {' · '.join(parts)}"


def _state(server: Server) -> str:
    if server.online is None:
        return _("no readings yet")
    return _("online") if server.online else _("does not answer")


def _load(sample: ServerSample) -> list[str]:
    readings = (
        (_("CPU"), sample.cpu_percent),
        (_("memory"), sample.memory_percent),
        (_("disk"), sample.disk_percent),
    )
    return [f"{label} {value:.0f}%" for label, value in readings if value is not None]


def _incident_line(incident: Incident) -> str:
    return _("%(server)s since %(time)s, reports: %(reports)s") % {
        "server": incident.server.name,
        "time": _time(incident),
        "reports": getattr(incident, "reports", 0),
    }


def _time(incident: Incident) -> str:
    return formats.date_format(timezone.localtime(incident.opened_at), "SHORT_DATETIME_FORMAT")
