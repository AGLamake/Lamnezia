"""What needs a staff member's attention now: the counters of the admin menu and the dashboard list.

Everything is counted within the member's zone and only for the sections they may see
(apps/accounts/services/access.py); what spans every server (applications, relays and groups,
failed operations of others) needs the whole zone, and the admin site and panel updates are an
administrator's.
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
from apps.accounts.services.access import Access, Section
from apps.accounts.services.applications import Applications
from apps.accounts.services.zone import Zone
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.membership import join_failed, leave_failed
from apps.guides.models import Suggestion, UsefulMaterial
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.relays.models import Relay
from apps.servers.models import InterfaceRepair, Server
from apps.support.services.inbox import Inbox
from apps.traffic.models import TrafficSource
from apps.updates.services.releases import PanelRelease
from apps.updates.services.updates import PanelUpdate

# A VPS paid for fewer days than this is worth a reminder.
PAYMENT_SOON_DAYS = 7
# Failed operations older than this are history, not something to look at.
_FAILED_WITHIN = timedelta(days=1)


@dataclass(frozen=True)
class Notice:
    """One line of the "needs attention" list: how bad it is, what happened and where to go.

    `key` names the notice for hiding it until tomorrow (apps/dashboard/services/snooze.py);
    `external` is an outside page that helps settle it, such as the hosting provider's billing.
    """

    level: str
    icon: str
    text: str
    url: str
    key: str
    external: str = ""


class Attention:
    """Counters and notices of one staff member, each query run once per request."""

    def __init__(self, member: User, now: datetime | None = None) -> None:
        self._member = member
        self._now = now or timezone.now()
        self._access = Access(member)
        self._zone = Zone(member)

    @cached_property
    def servers(self) -> list[Server]:
        servers: QuerySet[Server] = (
            Server.objects.select_related("latest_sample")
            .prefetch_related("load_alerts", "interface_repairs")
            .order_by("name")
        )
        return list(self._zone.narrow(servers, server="pk"))

    @cached_property
    def unread(self) -> int:
        return Inbox(self._member).unread() if self._access.allows(Section.INBOX) else 0

    @cached_property
    def applications(self) -> int:
        if not (self._access.allows(Section.REGISTRATION) and self._zone.is_whole):
            return 0
        return Applications.waiting().count()

    @cached_property
    def suggestions(self) -> int:
        if not self._access.allows(Section.INBOX):
            return 0
        return Suggestion.objects.pending().count()

    @cached_property
    def failed_operations(self) -> int:
        return self.failed_since(self._now - _FAILED_WITHIN)

    def failed_since(self, start: datetime) -> int:
        """Operations failed since `start`; outside the whole zone only the team's count."""
        if not self._access.allows(Section.OPERATIONS):
            return 0
        failed = Operation.objects.filter(status=OperationStatus.FAILED, created_at__gte=start)
        if not self._zone.is_whole:
            failed = failed.filter(created_by__in=self._zone.team())
        return failed.count()

    @cached_property
    def broken_links(self) -> int:
        # Materials are edited in the admin site, which only administrators open.
        return UsefulMaterial.objects.broken().count() if self._zone.is_whole else 0

    @cached_property
    def relays(self) -> list[Relay]:
        """Installed relays; they serve groups across servers, so they need the whole zone."""
        if not self._sees_every_server:
            return []
        relays = (
            Relay.objects.filter(installed_at__isnull=False)
            .select_related("server")
            .prefetch_related("server__relayed_groups")
        )
        return list(relays.order_by("server__name"))

    def unread_relays(self) -> list[Relay]:
        return [relay for relay in self.relays if relay.read_error]

    def mismatched_relays(self) -> list[Relay]:
        """Relays that keep other settings after the panel wrote them again."""
        return [relay for relay in self.relays if not relay.read_error and relay.mismatch_stays]

    def groups_down(self) -> list[ServerGroup]:
        """Groups whose relay saw no member answer; a failed reading is not trusted."""
        return [
            group
            for relay in self.relays
            if not relay.read_error
            for group in relay.server.relayed_groups.all()
            if (health := relay.group_health(group.port)) and health.all_down
        ]

    @cached_property
    def group_problems(self) -> int:
        """Groups to look at: no member answers the relay, or a member lags or is stuck joining
        or leaving."""
        if not self._sees_every_server:
            return 0
        troubled = {group.pk for group in self.groups_down()}
        troubled.update(
            GroupMember.objects.filter(lagging_since__isnull=False).values_list(
                "group_id", flat=True
            )
        )
        leaving = GroupMember.objects.filter(state=MemberState.LEAVING)
        troubled.update(member.group_id for member in leaving if leave_failed(member))
        joining = GroupMember.objects.filter(state=MemberState.JOINING)
        troubled.update(member.group_id for member in joining if join_failed(member))
        return len(troubled)

    @cached_property
    def panel_release(self) -> PanelRelease | None:
        """A release newer than the running panel; updating the panel is an administrator's."""
        if not self._zone.is_whole:
            return None
        return PanelUpdate.from_settings().offer().release

    def offline(self) -> list[Server]:
        return [server for server in self.servers if server.online is False]

    def overloaded(self) -> list[Server]:
        """Answering servers with a resource above its load threshold for a while."""
        return [server for server in self.servers if server.online and server.overloads]

    def failed_repairs(self) -> list[InterfaceRepair]:
        """Containers still without their VPN interface after every restart the monitor tried."""
        return [
            repair
            for server in self.servers
            for repair in server.interface_repairs.all()
            if repair.gave_up
        ]

    def without_ifb(self) -> list[Server]:
        """Servers whose speed limits drop upload over them: the host has no ifb module."""
        sources = TrafficSource.objects.filter(upload_policed=True).values_list("source", flat=True)
        pks = [pk for source in sources if (pk := ProtocolInstance.pk_of(source)) is not None]
        if not pks:
            return []
        ids = set(
            ProtocolInstance.objects.filter(
                pk__in=pks, container_removed_at__isnull=True
            ).values_list("server_id", flat=True)
        )
        return [server for server in self.servers if server.pk in ids]

    def payment_due(self) -> list[Server]:
        if not self._access.allows(Section.SERVERS):
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
        notices += [
            Notice(
                "danger",
                "server",
                _("%(name)s: %(container)s has no VPN interface, restarts did not help")
                % {"name": repair.server.name, "container": repair.container},
                self._server_url(repair.server),
                f"interface-repair:{repair.pk}",
            )
            for repair in self.failed_repairs()
        ]
        notices += [
            Notice(
                "danger",
                "server",
                _("Group %(name)s: no member answers the relay") % {"name": group.name},
                reverse("groups:detail", args=[group.pk]),
                f"group-down:{group.pk}",
            )
            for group in self.groups_down()
        ]
        notices += [
            Notice(
                "warning",
                "server",
                _("Relay %(name)s: its state cannot be read") % {"name": relay.server.name},
                reverse("servers:detail", args=[relay.server_id]),
                f"relay-unread:{relay.pk}",
            )
            for relay in self.unread_relays()
        ]
        notices += [
            Notice(
                "warning",
                "server",
                _("Relay %(name)s: other settings stay after the panel wrote it again")
                % {"name": relay.server.name},
                reverse("servers:detail", args=[relay.server_id]),
                f"relay-mismatch:{relay.pk}",
            )
            for relay in self.mismatched_relays()
        ]
        notices += [self._overload(server) for server in self.overloaded()]
        notices += [
            Notice(
                "warning",
                "server",
                _("%(name)s: no ifb module, upload over the speed limits is dropped")
                % {"name": server.name},
                self._server_url(server),
                f"no-ifb:{server.pk}",
            )
            for server in self.without_ifb()
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
        if self.panel_release:
            version = self.panel_release.version
            notices.append(
                Notice(
                    "info",
                    "arrow-up",
                    _("Lamnezia %(version)s is released") % {"version": version},
                    reverse("updates:update"),
                    f"panel-release:{version}",
                )
            )
        return notices

    def _overload(self, server: Server) -> Notice:
        resources = ", ".join(str(alert.get_resource_display()) for alert in server.overloads)
        text = _("%(name)s: high load — %(resources)s")
        return Notice(
            "warning",
            "cpu",
            text % {"name": server.name, "resources": resources},
            self._server_url(server),
            f"overload:{server.pk}",
        )

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
            server.payment_url,
        )

    @cached_property
    def _sees_every_server(self) -> bool:
        return self._access.allows(Section.SERVERS) and self._zone.is_whole

    def _server_url(self, server: Server) -> str:
        if self._access.allows(Section.SERVERS):
            return reverse("servers:detail", args=[server.pk])
        return reverse("dashboard:index") + f"#server-{server.pk}"
