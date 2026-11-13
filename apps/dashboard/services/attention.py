"""What needs a staff member's attention now: the counters of the admin menu and the dashboard list.

Everything is counted within the member's zone; the parts only an administrator handles
(applications, VPS payments, broken links, failed operations of others) are an administrator's.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property

from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.accounts.models import User
from apps.accounts.services.applications import Applications
from apps.accounts.services.zone import Zone
from apps.guides.models import Suggestion, UsefulMaterial
from apps.operations.models import Operation, OperationStatus
from apps.servers.models import Server
from apps.support.services.inbox import Inbox

# A VPS paid for fewer days than this is worth a reminder.
PAYMENT_SOON_DAYS = 7
# Failed operations older than this are history, not something to look at.
_FAILED_WITHIN = timedelta(days=1)


@dataclass(frozen=True)
class Notice:
    """One line of the "needs attention" list: how bad it is, what happened and where to go.

    `key` names the notice for hiding it until tomorrow (apps/dashboard/services/snooze.py).
    """

    level: str
    icon: str
    text: str
    url: str
    key: str


class Attention:
    """Counters and notices of one staff member, each query run once per request."""

    def __init__(self, member: User, now: datetime | None = None) -> None:
        self._member = member
        self._now = now or timezone.now()

    @cached_property
    def servers(self) -> list[Server]:
        servers: QuerySet[Server] = Server.objects.select_related("latest_sample").order_by("name")
        return list(Zone(self._member).narrow(servers, server="pk"))

    @cached_property
    def unread(self) -> int:
        return Inbox(self._member).unread()

    @cached_property
    def applications(self) -> int:
        return Applications.waiting().count() if self._member.is_admin else 0

    @cached_property
    def suggestions(self) -> int:
        return Suggestion.objects.pending().count()

    @cached_property
    def failed_operations(self) -> int:
        failed = Operation.objects.filter(
            status=OperationStatus.FAILED, created_at__gte=self._now - _FAILED_WITHIN
        )
        if not self._member.is_admin:
            failed = failed.filter(created_by=self._member)
        return failed.count()

    @cached_property
    def broken_links(self) -> int:
        # Materials are edited in the admin site, which only administrators open.
        return UsefulMaterial.objects.broken().count() if self._member.is_admin else 0

    def offline(self) -> list[Server]:
        return [server for server in self.servers if server.online is False]

    def payment_due(self) -> list[Server]:
        if not self._member.is_admin:
            return []
        return [
            server
            for server in self.servers
            if server.days_until_paid is not None and server.days_until_paid <= PAYMENT_SOON_DAYS
        ]

    def notices(self) -> list[Notice]:
        """The list on the dashboard, the most urgent first."""
        notices = [
            Notice(
                "danger",
                "server",
                _("%(name)s does not answer") % {"name": server.name},
                self._server_url(server),
                f"offline:{server.pk}",
            )
            for server in self.offline()
        ]
        notices += [self._payment(server) for server in self.payment_due()]
        if self.failed_operations:
            notices.append(
                Notice(
                    "danger",
                    "activity",
                    ngettext(
                        "%(count)d operation failed in the last day",
                        "%(count)d operations failed in the last day",
                        self.failed_operations,
                    )
                    % {"count": self.failed_operations},
                    reverse("operations:list") + "?status=failed",
                    "failed-operations",
                )
            )
        if self.unread:
            notices.append(
                Notice(
                    "info",
                    "inbox",
                    ngettext("%(count)d unread message", "%(count)d unread messages", self.unread)
                    % {"count": self.unread},
                    reverse("support:inbox"),
                    "unread",
                )
            )
        if self.applications:
            notices.append(
                Notice(
                    "info",
                    "user-plus",
                    ngettext(
                        "%(count)d application waits for approval",
                        "%(count)d applications wait for approval",
                        self.applications,
                    )
                    % {"count": self.applications},
                    reverse("accounts:registrations"),
                    "applications",
                )
            )
        if self.suggestions:
            notices.append(
                Notice(
                    "info",
                    "file-text",
                    ngettext(
                        "%(count)d article suggestion to review",
                        "%(count)d article suggestions to review",
                        self.suggestions,
                    )
                    % {"count": self.suggestions},
                    reverse("guides:moderation"),
                    "suggestions",
                )
            )
        if self.broken_links:
            notices.append(
                Notice(
                    "warning",
                    "external",
                    ngettext(
                        "%(count)d useful material is hidden: its link is broken",
                        "%(count)d useful materials are hidden: their links are broken",
                        self.broken_links,
                    )
                    % {"count": self.broken_links},
                    reverse("admin:guides_usefulmaterial_changelist") + "?is_broken__exact=1",
                    "broken-links",
                )
            )
        return notices

    def _payment(self, server: Server) -> Notice:
        overdue = server.days_overdue
        days = server.days_until_paid or 0
        if overdue:
            text = ngettext(
                "%(name)s: VPS payment overdue by %(days)d day",
                "%(name)s: VPS payment overdue by %(days)d days",
                overdue,
            ) % {"name": server.name, "days": overdue}
        elif days == 0:
            text = _("%(name)s: VPS payment is due today") % {"name": server.name}
        else:
            text = ngettext(
                "%(name)s: pay for the VPS within %(days)d day",
                "%(name)s: pay for the VPS within %(days)d days",
                days,
            ) % {"name": server.name, "days": days}
        return Notice(
            "danger" if overdue else "warning",
            "wallet",
            text,
            self._server_url(server),
            f"payment:{server.pk}",
        )

    def _server_url(self, server: Server) -> str:
        if self._member.is_admin:
            return reverse("servers:detail", args=[server.pk])
        return reverse("dashboard:index") + f"#server-{server.pk}"
