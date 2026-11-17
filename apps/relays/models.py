"""The relay role of a server and the service peer of a group (docs/FALLBACK.md section 5).

A relay forwards the UDP port of every group that names its server to the live member of the
group; its agent probes the members with the service peer of the group.
"""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from amnezia_core.relay import DEFAULT_CHECK_INTERVAL, DEFAULT_FAILURES
from apps.relays.services.health import GroupHealth, RelayHealth
from apps.relays.services.mismatch import RelayMismatch
from apps.ssh.encryption import EncryptedTextField

MAX_CHECK_INTERVAL = 60
MAX_FAILURES = 10
SKIP_DETAIL_LENGTH = 255


class SyncSkip(models.TextChoices):
    """Why a sync does not update a relay: an update would fail the same way."""

    NONE = "", _("not skipped")
    MEMBER_ON_RELAY = "member_on_relay", _("a member runs on the relay server")
    HOST_NOT_RESOLVED = "host_not_resolved", _("the name of a member does not resolve")


class Relay(models.Model):
    """A server that runs the relay container; `installed_at` is empty until it is installed."""

    server = models.OneToOneField(
        "servers.Server",
        verbose_name=_("server"),
        on_delete=models.CASCADE,
        related_name="relay_role",
    )
    installed_at = models.DateTimeField(_("installed at"), null=True, blank=True)
    # Digest of the groups and backends the relay last got; a sync compares it with the panel.
    synced = models.CharField(_("synced digest"), max_length=64, blank=True)
    synced_at = models.DateTimeField(_("synced at"), null=True, blank=True)
    # The agent state as the monitor last read it, with the members matched (services/health.py).
    health = models.JSONField(_("health"), default=dict, blank=True)
    read_at = models.DateTimeField(_("state read at"), null=True, blank=True)
    # Why the last reading failed: an SSH error code, "bad_state" or "stale"; empty when it worked.
    read_error = models.CharField(_("reading error"), max_length=32, blank=True)
    # What the last trusted reading forwards apart from what the panel wrote (services/mismatch.py).
    mismatch = models.JSONField(_("settings mismatch"), default=dict, blank=True)
    # Bytes per second between the last two readings; empty when unknown.
    rate_from_clients = models.FloatField(_("bytes per second from clients"), null=True, blank=True)
    rate_to_clients = models.FloatField(_("bytes per second to clients"), null=True, blank=True)
    # How the agent probes the members; an update writes them to the relay (amnezia_core/relay.py).
    check_interval = models.PositiveSmallIntegerField(
        _("check interval, seconds"),
        default=DEFAULT_CHECK_INTERVAL,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_CHECK_INTERVAL)],
        help_text=_("How often the relay probes each member of a group."),
    )
    failures = models.PositiveSmallIntegerField(
        _("failures before a switch"),
        default=DEFAULT_FAILURES,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_FAILURES)],
        help_text=_(
            "Failed probes in a row after which the relay moves the group to the next member."
        ),
    )
    # Why the last sync left the relay as it is although the panel sees other groups.
    sync_skipped = models.CharField(
        _("sync skipped"), max_length=32, choices=SyncSkip, blank=True, default=SyncSkip.NONE
    )
    sync_skipped_detail = models.CharField(
        _("why the sync skipped"), max_length=SKIP_DETAIL_LENGTH, blank=True
    )

    class Meta:
        verbose_name = _("relay")
        verbose_name_plural = _("relays")

    def __str__(self) -> str:
        return str(self.server)

    @property
    def installed(self) -> bool:
        return self.installed_at is not None

    @property
    def settings_mismatch(self) -> RelayMismatch:
        return RelayMismatch.load(self.mismatch)

    @property
    def groups_health(self) -> list[GroupHealth]:
        """The groups of the last reading by port, but for groups the panel did not give the
        relay; empty before a reading."""
        reading = RelayHealth.load(self.health)
        if reading is None:
            return []
        foreign = set(self.settings_mismatch.foreign_ports)
        return [group for port, group in sorted(reading.groups.items()) if port not in foreign]

    def group_health(self, port: int) -> GroupHealth | None:
        return next((group for group in self.groups_health if group.port == port), None)


class ServicePeer(models.Model):
    """The client of a group the relay probes its members with; it is never given to a user.

    Every member has it as a peer, like the keys of the group.
    """

    group = models.OneToOneField(
        "groups.ServerGroup",
        verbose_name=_("group"),
        on_delete=models.CASCADE,
        related_name="service_peer",
    )
    private_key = EncryptedTextField(_("private key"))
    public_key = models.CharField(_("public key"), max_length=64, unique=True)
    address = models.GenericIPAddressField(_("tunnel address"), protocol="IPv4")

    class Meta:
        verbose_name = _("service peer")
        verbose_name_plural = _("service peers")

    def __str__(self) -> str:
        return f"{self.group} · {self.address}"


class RelaySample(models.Model):
    """The channel load of one reading of the relay, kept for the load chart of its card."""

    relay = models.ForeignKey(Relay, on_delete=models.CASCADE, related_name="samples")
    at = models.DateTimeField(_("read at"), db_index=True)
    rate_from_clients = models.FloatField(_("bytes per second from clients"))
    rate_to_clients = models.FloatField(_("bytes per second to clients"))

    class Meta:
        verbose_name = _("relay reading")
        verbose_name_plural = _("relay readings")
        ordering = ("-at",)
        indexes = (models.Index(fields=["relay", "at"]),)

    def __str__(self) -> str:
        return f"{self.relay_id} @ {self.at:%Y-%m-%d %H:%M}"
