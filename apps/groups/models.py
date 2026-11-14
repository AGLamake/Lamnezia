"""Server groups: AmneziaWG servers that are clones of one leader behind a relay.

docs/FALLBACK.md section 4. A protocol instance is either standalone or a member of one group:
the one-to-one link of `GroupMember` makes the modes exclusive.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class GroupProtocol(models.TextChoices):
    AWG3 = "awg3", _("AmneziaWG 3.1")


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
