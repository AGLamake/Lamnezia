"""Server status and the quick "a server does not work" report (DESIGN.md 6.7), the same as on
the portal: the report starts a check of the server and joins its incident.
"""

from collections.abc import Mapping
from functools import cached_property

from django.utils.translation import gettext as _

from apps.bots.commands.base import CommandSet, Handler, bullets, nav, number, unavailable
from apps.bots.services.replies import Button, Command, Reply
from apps.plans.services.limits import effective_limits
from apps.portal.services.servers import ServerState, my_servers
from apps.servers.models import Server
from apps.support.services.thread import TooManyMessagesError
from apps.support.services.tickets import MyRequests, ServerNotAllowedError, TooManyRequestsError


class ServerCommands(CommandSet):
    def handlers(self) -> Mapping[str, Handler]:
        return {"servers": self.servers, "down": self.report}

    @cached_property
    def states(self) -> list[ServerState]:
        return my_servers(effective_limits(self.user))

    def servers(self, command: Command) -> Reply:
        title = _("Servers")
        if not self.states:
            return Reply(_("Your plan has no servers yet."), nav(), title=title)
        lines = [
            f"{state.name}{f' ({state.location})' if state.location else ''}: {state.health.label}"
            for state in self.states
        ]
        return Reply(
            bullets(lines), (Button(_("A server does not work"), "down"), *nav()), title=title
        )

    def report(self, command: Command) -> Reply:
        """`down`: choose the server; `down <id>`: wait for a comment; `down <id> send` or the
        typed comment: report it.
        """
        if not command.args:
            return self._choose()
        state = next((s for s in self.states if s.pk == number(command.arg(0))), None)
        if state is None:
            return unavailable()
        if not command.text and command.arg(1) != "send":
            self.chat.wait_for_text(Command("down", (str(state.pk),)))
            return Reply(
                _(
                    "What does not work on %(server)s? Write it in one message, or send the "
                    "report as is."
                )
                % {"server": state.name},
                (Button(_("Send without a comment"), f"down {state.pk} send"), *nav("down")),
                title=_("A server does not work"),
            )
        return self._send(state, command.text)

    def _choose(self) -> Reply:
        title = _("A server does not work")
        if not self.states:
            return Reply(_("Your plan has no servers yet."), nav(), title=title)
        return Reply(
            _("Which server does not work?"),
            (*(Button(state.name, f"down {state.pk}") for state in self.states), *nav()),
            title=title,
        )

    def _send(self, state: ServerState, comment: str) -> Reply:
        server = Server.objects.get(pk=state.pk)
        try:
            MyRequests(self.user, self.chat.client, self.chat.messenger).report_server(
                server, comment
            )
        except ServerNotAllowedError:
            return unavailable()
        except (TooManyRequestsError, TooManyMessagesError):
            return Reply(_("Too many requests in an hour. Please try again later."), nav())
        return Reply(
            _("Thank you. The panel is checking the server, and the administrators know of it."),
            nav(),
            title=_("A server does not work"),
        )
