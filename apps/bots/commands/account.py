"""The menu, the user's access status, guides and unlinking the messenger account."""

from collections.abc import Mapping
from datetime import datetime

from django.template.defaultfilters import filesizeformat
from django.utils import formats, timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Status
from apps.accounts.services.linking import AccountLinks
from apps.audit.services import journal as audit
from apps.bots.commands.base import CommandSet, Handler, bullets, nav
from apps.bots.services.replies import Button, Command, Reply
from apps.guides.services.devices import Device
from apps.plans.services.limits import effective_limits
from apps.portal.services.usage import Usage


class AccountCommands(CommandSet):
    open_to_all = frozenset({"start", "menu", "help", "status", "guides", "unlink"})

    def handlers(self) -> Mapping[str, Handler]:
        return {
            "start": self.menu,
            "menu": self.menu,
            "help": self.help,
            "status": self.status,
            "guides": self.guides,
            "unlink": self.unlink,
        }

    def menu(self, command: Command) -> Reply:
        title = _("Menu")
        if not self.user.can_have_keys:
            buttons = (Button(_("Status"), "status"), Button(_("Guides"), "guides"))
            return Reply(_("What would you like to do?"), buttons, title=title)
        staff = (Button(_("Administration"), "admin"),) if self.user.is_staff_member else ()
        return Reply(
            _("What would you like to do?"),
            (
                Button(_("Status"), "status"),
                Button(_("My keys"), "keys"),
                Button(_("New key"), "newkey"),
                Button(_("Servers"), "servers"),
                Button(_("A server does not work"), "down"),
                Button(_("Support"), "support"),
                Button(_("Guides"), "guides"),
                *staff,
            ),
            title=title,
        )

    def help(self, command: Command) -> Reply:
        return Reply(
            _(
                "/status: traffic and access term\n"
                "/keys: your keys\n"
                "/newkey: get a key\n"
                "/sub: subscription link for XRay clients\n"
                "/servers: server status\n"
                "/down: report a server that does not work\n"
                "/support: write to the administrators\n"
                "/ticket: open a request\n"
                "/requests: your requests and the answers\n"
                "/guides: how to set up the app\n"
                "/unlink: unlink this account from the panel"
            ),
            nav(),
            title=_("Commands"),
        )

    def status(self, command: Command) -> Reply:
        title = _("Status")
        if self.user.status == Status.PENDING:
            text = _("Your registration waits for approval. Meanwhile you can read the guides.")
            return Reply(text, (Button(_("Guides"), "guides"), *nav()), title=title)
        if self.user.status == Status.BLOCKED:
            text = _("Your account is blocked. Write to the administrator.")
            return Reply(text, nav(), title=title)
        limits = effective_limits(self.user)
        if not limits.has_plan:
            text = _("You have no plan yet. Ask the administrator for one.")
            return Reply(text, nav(), title=title)
        usage = Usage.of(self.user, limits, timezone.now())
        return Reply(
            bullets(_usage_lines(usage)), (Button(_("My keys"), "keys"), *nav()), title=title
        )

    def guides(self, command: Command) -> Reply:
        links = self.chat.links
        devices = tuple(
            Button(device.label, url=links.to("guides:platform", device.value)) for device in Device
        )
        return Reply(
            _("Choose your device to download the Amnezia app and set it up."),
            (*devices, Button(_("Questions and answers"), url=links.to("guides:faq")), *nav()),
            title=_("Guides"),
        )

    def unlink(self, command: Command) -> Reply:
        if command.arg(0) != "yes":
            return Reply(
                _("Unlink this account from the panel? The bot will stop answering you here."),
                (Button(_("Unlink"), "unlink yes"), *nav()),
                title=_("Unlink"),
            )
        AccountLinks().unlink(self.user, self.chat.messenger)
        audit.log(
            "accounts.unlink",
            actor=self.user,
            target=self.user,
            data={"messenger": self.chat.messenger, "via": "bot"},
        )
        return Reply(_("Unlinked. You can link the account again in your profile."))


def _usage_lines(usage: Usage) -> list[str]:
    lines = [_traffic_line(usage)]
    if usage.total_limit is not None:
        lines.append(
            _("Total traffic: %(used)s of %(limit)s.")
            % {"used": filesizeformat(usage.total_used), "limit": filesizeformat(usage.total_limit)}
        )
    if usage.access_until is None:
        lines.append(_("Access has no end date."))
    elif usage.expired:
        lines.append(_("Access ended on %(date)s.") % {"date": _date(usage.access_until)})
    else:
        lines.append(_("Access until %(date)s.") % {"date": _date(usage.access_until)})
    return lines


def _traffic_line(usage: Usage) -> str:
    used = filesizeformat(usage.period_used)
    if usage.period_limit is None:
        return _("Traffic: %(used)s, no limit.") % {"used": used}
    line = _("Traffic: %(used)s of %(limit)s.") % {
        "used": used,
        "limit": filesizeformat(usage.period_limit),
    }
    if usage.resets_at is not None:
        line += " " + _("Resets on %(date)s.") % {"date": _date(usage.resets_at)}
    return line


def _date(value: datetime) -> str:
    return formats.date_format(timezone.localtime(value), "DATE_FORMAT")
