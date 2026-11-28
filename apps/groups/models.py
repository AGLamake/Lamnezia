"""Server groups: AmneziaWG servers that are clones of one leader behind a relay.

docs/FALLBACK.md section 4. A protocol instance is either standalone or a member of one group:
the one-to-one link of `GroupMember` makes the modes exclusive.
"""

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class GroupProtocol(models.TextChoices):
    AWG3 = "awg3", _("AmneziaWG 3.1")


class GroupRouting(models.TextChoices):
    """How the relay carries the group (docs/SPLIT.md): forwards the port or splits the traffic."""

    FORWARD = "forward", _("forward")
    SPLIT = "split", _("split")


class GroupListMode(models.TextChoices):
    """What the network list of a split group holds (`amnezia_core.split_lists.ListMode`)."""

    EXCLUDE = "exclude", _("blacklist: abroad all but the list")
    INCLUDE = "include", _("whitelist: abroad only the list")


class ServerGroup(models.Model):
    """Servers with one identity: the server key, [Interface], subnet, port and peers of the leader.

    Clients reach the group through the relay on `port`, by `domain` or the relay address.
    """

    name = models.CharField(_("name"), max_length=100, unique=True)
    protocol = models.CharField(
        _("protocol"), max_length=16, choices=GroupProtocol, default=GroupProtocol.AWG3
    )
    port = models.PositiveIntegerField(_("port"))
    domain = models.CharField(
        _("domain"),
        max_length=253,
        blank=True,
        help_text=_("Host name whose A record points to the relay; keys of the group use it."),
    )
    relay = models.ForeignKey(
        "servers.Server",
        verbose_name=_("relay"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="relayed_groups",
    )
    routing = models.CharField(
        _("routing"), max_length=16, choices=GroupRouting, default=GroupRouting.FORWARD
    )
    list_mode = models.CharField(
        _("list mode"), max_length=16, choices=GroupListMode, default=GroupListMode.EXCLUDE
    )
    dns_all = models.BooleanField(
        _("answer all DNS"),
        default=False,
        help_text=_(
            "The relay's resolver answers every DNS query of the clients on port 53, whatever "
            "server a device asks."
        ),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("server group")
        verbose_name_plural = _("server groups")
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @property
    def leader(self) -> "GroupMember | None":
        return self.members.filter(leading=True).select_related("instance__server").first()


class MemberState(models.TextChoices):
    JOINING = "joining", _("joining")
    MEMBER = "member", _("member")
    LEAVING = "leaving", _("leaving")


class LagReason(models.TextChoices):
    """Why a member lags behind its group; the group page shows it."""

    NONE = "", _("not lagging")
    UNREACHABLE = "unreachable", _("was unreachable during a change")
    MISSED = "missed", _("missed a change of the keys or the settings")
    DRIFT = "drift", _("its files differ from the leading server")
    STAYED = "stayed", _("stayed in the group after a failed leave")


class MemberQuerySet(models.QuerySet["GroupMember"]):
    def fall_behind(self, reason: LagReason) -> int:
        """Mark the members that are up to date as lagging for `reason`; returns how many."""
        return self.filter(lagging_since=None).update(
            lagging_since=timezone.now(), lagging_reason=reason
        )


class GroupMember(models.Model):
    """A protocol instance in a group; the lower `priority`, the earlier the relay picks it."""

    group = models.ForeignKey(
        ServerGroup, verbose_name=_("group"), on_delete=models.CASCADE, related_name="members"
    )
    instance = models.OneToOneField(
        "protocols.ProtocolInstance",
        verbose_name=_("protocol"),
        on_delete=models.CASCADE,
        related_name="group_member",
    )
    priority = models.PositiveIntegerField(_("priority"))
    # The server whose files are the identity of the group; its keys stay with the group.
    leading = models.BooleanField(_("leading server"), default=False)
    state = models.CharField(
        _("state"), max_length=16, choices=MemberState, default=MemberState.JOINING
    )
    joined_at = models.DateTimeField(_("joined at"), null=True, blank=True)
    # Since when the server misses key changes of the group: it was unreachable when one was
    # made. The relay skips it until the catch-up copies the group files onto it.
    lagging_since = models.DateTimeField(_("lagging since"), null=True, blank=True)
    lagging_reason = models.CharField(
        _("why it lags"), max_length=16, choices=LagReason, default=LagReason.NONE, blank=True
    )

    objects = MemberQuerySet.as_manager()

    class Meta:
        verbose_name = _("group member")
        verbose_name_plural = _("group members")
        ordering = ("group", "priority", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=["group"],
                condition=models.Q(leading=True),
                name="groups_member_one_leader",
            ),
        )

    def __str__(self) -> str:
        return f"{self.group} · {self.instance.server}"

    @property
    def joined(self) -> bool:
        return self.state == MemberState.MEMBER

    @property
    def lagging(self) -> bool:
        return self.lagging_since is not None
