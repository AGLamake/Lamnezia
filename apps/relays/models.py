"""The relay role of a server and the service peers of a group (docs/FALLBACK.md section 5).

A relay forwards the UDP port of every group that names its server to the live member of the
group; its agent probes the members with the probe peer of the group. A split group also has
an uplink peer and its network list: sources and own entries (docs/SPLIT.md).
"""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from amnezia_core.relay import DEFAULT_CHECK_INTERVAL, DEFAULT_FAILURES, ResolverState
from apps.relays.errors import RelayErrorCode
from apps.relays.services.health import GroupHealth, RelayHealth
from apps.relays.services.image import IMAGE_VERSION, agent_digest
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
    # The image version the container was last built with and the digest of the agent the
    # panel last wrote (services/image.py); a relay of an older release has 0 and empty.
    image_version = models.PositiveSmallIntegerField(_("image version"), default=0)
    agent_digest = models.CharField(_("agent digest"), max_length=64, blank=True)
    # Digest of the groups and backends the relay last got; a sync compares it with the panel.
    synced = models.CharField(_("synced digest"), max_length=64, blank=True)
    synced_at = models.DateTimeField(_("synced at"), null=True, blank=True)
    # Digest of the clone peers of the split groups the relay last got (services/clone.py).
    peers_synced = models.CharField(_("synced peers digest"), max_length=64, blank=True)
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
    def image_outdated(self) -> bool:
        """The container was built from an older image: only a reinstall brings what split
        routing needs."""
        return self.installed and self.image_version < IMAGE_VERSION

    @property
    def split_refusal(self) -> RelayErrorCode | None:
        """Why a group cannot be split on this relay now: an older image, or an agent the
        panel has not written since its release; None when it can."""
        if self.image_outdated:
            return RelayErrorCode.SPLIT_IMAGE_OUTDATED
        if self.agent_digest != agent_digest():
            return RelayErrorCode.SPLIT_AGENT_OUTDATED
        return None

    @property
    def settings_mismatch(self) -> RelayMismatch:
        return RelayMismatch.load(self.mismatch)

    @property
    def mismatch_stays(self) -> bool:
        """The mismatch was judged again after the panel wrote the relay: the write did not
        mend it."""
        written, read = self.synced_at, self.read_at
        return bool(self.mismatch and self.synced and written and read and read > written)

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

    @property
    def resolver(self) -> ResolverState:
        """The resolver of the split groups' clients as of the last reading; `unknown` before
        one."""
        reading = RelayHealth.load(self.health)
        return reading.resolver if reading else ResolverState()

    @property
    def splits(self) -> bool:
        """A group of the last reading is in split routing: the relay works for it."""
        return any(group.split for group in self.groups_health)


class PeerRole(models.TextChoices):
    """What a service peer of a group is for; neither is ever given to a user."""

    PROBE = "probe", _("probe")
    UPLINK = "uplink", _("uplink")


class ServicePeer(models.Model):
    """A client of a group that the relay holds; every member has it as a peer, like the keys.

    The probe is how the agent checks the members. The uplink carries the traffic of a split
    group from the clone on the relay to the active member (docs/SPLIT.md 4.7). A group has at
    most one of each.
    """

    group = models.ForeignKey(
        "groups.ServerGroup",
        verbose_name=_("group"),
        on_delete=models.CASCADE,
        related_name="service_peers",
    )
    role = models.CharField(_("role"), max_length=16, choices=PeerRole, default=PeerRole.PROBE)
    private_key = EncryptedTextField(_("private key"))
    public_key = models.CharField(_("public key"), max_length=64, unique=True)
    address = models.GenericIPAddressField(_("tunnel address"), protocol="IPv4")

    class Meta:
        verbose_name = _("service peer")
        verbose_name_plural = _("service peers")
        constraints = (
            models.UniqueConstraint(fields=("group", "role"), name="service_peer_role_unique"),
        )

    def __str__(self) -> str:
        return f"{self.group} · {self.role} · {self.address}"


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


# Delegations of the RIPE NCC region by country, updated daily: the default source of the list.
RIPE_DELEGATIONS_URL = "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-latest"
SOURCE_ERROR_LENGTH = 255


class SourceKind(models.TextChoices):
    """The format of a source list (`amnezia_core.split_lists.SourceFormat`)."""

    CIDR = "cidr", _("networks, one per line")
    LIB4U = "lib4u", _("site list of the Amnezia application (JSON)")
    RIPE = "ripe", _("delegations of the RIPE NCC to Russia")


class SplitSource(models.Model):
    """A list of networks a split group sends straight out of the relay (docs/SPLIT.md 4.5).

    The panel downloads it daily and keeps the last good list: one that did not download, did
    not read or has less than half of the networks before is not taken.
    """

    group = models.ForeignKey(
        "groups.ServerGroup",
        verbose_name=_("group"),
        on_delete=models.CASCADE,
        related_name="split_sources",
    )
    url = models.URLField(_("address"), max_length=500)
    kind = models.CharField(_("format"), max_length=16, choices=SourceKind)
    enabled = models.BooleanField(_("enabled"), default=True)
    networks = models.TextField(_("networks"), blank=True, help_text=_("One network per line."))
    size = models.PositiveIntegerField(_("networks in the list"), default=0)
    updated_at = models.DateTimeField(_("list taken at"), null=True, blank=True)
    checked_at = models.DateTimeField(_("checked at"), null=True, blank=True)
    error = models.CharField(_("error"), max_length=SOURCE_ERROR_LENGTH, blank=True)
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta:
        verbose_name = _("network list source")
        verbose_name_plural = _("network list sources")
        ordering = ("pk",)
        constraints = (
            models.UniqueConstraint(fields=("group", "url"), name="split_source_unique"),
        )

    def __str__(self) -> str:
        return f"{self.group} · {self.url}"


class SplitDirection(models.TextChoices):
    """`amnezia_core.split_lists.Direction`."""

    LOCAL = "local", _("straight from the relay")
    ABROAD = "abroad", _("abroad through the uplink")


class SplitEntry(models.Model):
    """An own entry of a split group: a network or a domain that always goes one way.

    The relay agent resolves a domain itself every few minutes; the panel resolves none.
    """

    group = models.ForeignKey(
        "groups.ServerGroup",
        verbose_name=_("group"),
        on_delete=models.CASCADE,
        related_name="split_entries",
    )
    value = models.CharField(_("network or domain"), max_length=253)
    direction = models.CharField(_("direction"), max_length=16, choices=SplitDirection)
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta:
        verbose_name = _("own entry")
        verbose_name_plural = _("own entries")
        ordering = ("direction", "value")
        constraints = (
            models.UniqueConstraint(fields=("group", "value"), name="split_entry_unique"),
        )

    def __str__(self) -> str:
        return f"{self.group} · {self.direction} · {self.value}"
