from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.guides.models import LinkField, http_url
from apps.ssh.encryption import EncryptedTextField
from apps.ssh.services import SshCredentials

SSH_PORT = 22
# The reachability probe (services/probe.py): the monitor watches it with the Amnezia containers.
PROBE_CONTAINER = "lamnezia-probe"


class AuthMethod(models.TextChoices):
    KEY = "key", _("SSH key")
    PASSWORD = "password", _("password")


class ServerStatus(models.TextChoices):
    UNCHECKED = "unchecked", _("not checked")
    READY = "ready", _("ready")
    FAILED = "failed", _("check failed")


class CheckWarning(models.TextChoices):
    """Findings of a server check that do not stop the panel from using the server."""

    UNSUPPORTED_OS = (
        "unsupported_os",
        _("The panel can install protocols only on Debian and Ubuntu."),
    )
    NO_DOCKER = "no_docker", _("Docker is not installed: the panel installs it with a protocol.")
    LOW_DISK = "low_disk", _("Little free disk space: a protocol may fail to install.")


@dataclass(frozen=True)
class ServerFacts:
    """What a successful check found on the server."""

    os_name: str
    docker_version: str
    disk_free_bytes: int | None
    warnings: tuple[CheckWarning, ...] = ()


class Server(models.Model):
    """A VPS the panel manages over SSH. Credentials are stored encrypted and never shown again."""

    name = models.CharField(_("name"), max_length=100, unique=True)
    host = models.CharField(_("host"), max_length=255)
    port = models.PositiveIntegerField(
        _("SSH port"),
        default=SSH_PORT,
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
    )
    username = models.CharField(_("SSH user"), max_length=64, default="root")
    auth_method = models.CharField(
        _("login with"), max_length=16, choices=AuthMethod, default=AuthMethod.KEY
    )
    password = EncryptedTextField(_("password"), blank=True)
    private_key = EncryptedTextField(_("private key"), blank=True)
    passphrase = EncryptedTextField(_("key passphrase"), blank=True)
    location = models.CharField(
        _("location"),
        max_length=100,
        blank=True,
        help_text=_("Country or any label that tells servers apart, e.g. «Germany, Hetzner»."),
    )
    paid_until = models.DateField(_("VPS paid until"), null=True, blank=True)
    payment_url = LinkField(
        _("hosting or payment page"),
        max_length=500,
        blank=True,
        validators=[http_url],
        help_text=_("Where to pay for the VPS: the hosting provider's site or its billing page."),
    )
    maintenance = models.BooleanField(
        _("under maintenance"),
        default=False,
        help_text=_("Users see the server as under maintenance on their status page."),
    )
    # UDP port of the STUN responder clients' browsers probe; empty while it is not installed.
    probe_port = models.PositiveIntegerField(_("reachability probe port"), null=True, blank=True)
    status = models.CharField(
        _("status"), max_length=16, choices=ServerStatus, default=ServerStatus.UNCHECKED
    )
    os_name = models.CharField(_("operating system"), max_length=200, blank=True)
    docker_version = models.CharField(_("Docker"), max_length=100, blank=True)
    disk_free_bytes = models.PositiveBigIntegerField(_("free disk space"), null=True, blank=True)
    warnings = models.JSONField(_("warnings"), default=list, blank=True)
    # Amnezia containers seen by the latest import: [{"name", "port", "transport"}].
    containers = models.JSONField(_("Amnezia containers"), default=list, blank=True)
    checked_at = models.DateTimeField(_("checked at"), null=True, blank=True)
    # The monitor keeps the newest reading at hand for the server cards.
    latest_sample = models.ForeignKey(
        "ServerSample", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_seen_at = models.DateTimeField(_("last answered at"), null=True, blank=True)
    created_at = models.DateTimeField(_("added at"), auto_now_add=True)

    class Meta:
        verbose_name = _("server")
        verbose_name_plural = _("servers")
        ordering = ("name",)
        constraints = (
            models.UniqueConstraint(
                fields=["host", "port"],
                name="servers_server_unique_address",
                violation_error_message=_("A server with this host and port is already added."),
            ),
        )

    def __str__(self) -> str:
        return self.name

    def credentials(self) -> SshCredentials:
        if self.auth_method == AuthMethod.PASSWORD:
            return SshCredentials(self.host, self.port, self.username, password=self.password)
        return SshCredentials(
            self.host,
            self.port,
            self.username,
            private_key=self.private_key,
            passphrase=self.passphrase,
        )

    def record_check(self, facts: ServerFacts) -> None:
        self.status = ServerStatus.READY
        self.os_name, self.docker_version = facts.os_name, facts.docker_version
        self.disk_free_bytes = facts.disk_free_bytes
        self.warnings = [str(warning) for warning in facts.warnings]
        self._save_check()

    def record_failed_check(self) -> None:
        self.status = ServerStatus.FAILED
        self._save_check()

    @property
    def online(self) -> bool | None:
        """None until the first reading; offline after two monitor intervals without an answer."""
        if self.latest_sample is None:
            return None
        if self.last_seen_at is None:
            return False
        offline_after = timedelta(seconds=2 * settings.MONITOR_INTERVAL)
        return timezone.now() - self.last_seen_at < offline_after

    @property
    def days_until_paid(self) -> int | None:
        """Days left until the VPS must be paid; negative once overdue, None without a date."""
        if self.paid_until is None:
            return None
        return (self.paid_until - timezone.localdate()).days

    @property
    def days_overdue(self) -> int:
        """Days since the VPS payment date passed; 0 when it has not."""
        days = self.days_until_paid
        return -days if days is not None and days < 0 else 0

    @property
    def probe_down(self) -> bool:
        """The probe is installed, but the latest answered reading did not find it running.

        Every client would then see the server as unreachable, so the portal stops probing it.
        """
        sample = self.latest_sample
        if self.probe_port is None or sample is None or not sample.answered:
            return False
        return not sample.probe_running

    @property
    def overloads(self) -> list["LoadAlert"]:
        """Resources whose load is high now, in the order of `LoadResource`."""
        order = list(LoadResource.values)
        alerts = [alert for alert in self.load_alerts.all() if alert.high_since is not None]
        return sorted(alerts, key=lambda alert: order.index(alert.resource))

    def record_sample(self, sample: "ServerSample") -> None:
        self.latest_sample = sample
        if sample.answered:
            self.last_seen_at = sample.at
        self.save(update_fields=["latest_sample", "last_seen_at"])

    def record_containers(self, containers: list[dict[str, object]]) -> None:
        self.containers = containers
        self.save(update_fields=["containers"])

    def _save_check(self) -> None:
        self.checked_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "os_name",
                "docker_version",
                "disk_free_bytes",
                "warnings",
                "checked_at",
            ]
        )


class ServerSample(models.Model):
    """One monitor reading of a server. `error` is the SSH error code when it did not answer."""

    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="samples")
    at = models.DateTimeField(_("read at"), db_index=True)
    error = models.CharField(_("error"), max_length=32, blank=True)
    cpu_percent = models.FloatField(_("CPU, %"), null=True, blank=True)
    # Cumulative CPU ticks since boot: the next reading turns them into cpu_percent.
    cpu_total_ticks = models.PositiveBigIntegerField(null=True, blank=True)
    cpu_idle_ticks = models.PositiveBigIntegerField(null=True, blank=True)
    memory_total = models.PositiveBigIntegerField(_("memory"), null=True, blank=True)
    memory_available = models.PositiveBigIntegerField(_("free memory"), null=True, blank=True)
    disk_total = models.PositiveBigIntegerField(_("disk"), null=True, blank=True)
    disk_free = models.PositiveBigIntegerField(_("free disk space"), null=True, blank=True)
    uptime = models.PositiveBigIntegerField(_("uptime, s"), null=True, blank=True)
    # Amnezia containers and the probe: [{"name", "state"}], the state as `docker ps` reports it.
    containers = models.JSONField(_("containers"), default=list, blank=True)

    class Meta:
        verbose_name = _("server reading")
        verbose_name_plural = _("server readings")
        ordering = ("-at",)
        indexes = (models.Index(fields=["server", "at"]),)

    def __str__(self) -> str:
        return f"{self.server_id} @ {self.at:%Y-%m-%d %H:%M}"

    @property
    def answered(self) -> bool:
        return not self.error

    @property
    def memory_percent(self) -> float | None:
        return _used_percent(self.memory_total, self.memory_available)

    @property
    def disk_percent(self) -> float | None:
        return _used_percent(self.disk_total, self.disk_free)

    @property
    def booted_at(self) -> datetime | None:
        return None if self.uptime is None else self.at - timedelta(seconds=self.uptime)

    @property
    def stopped_containers(self) -> list[str]:
        return [c["name"] for c in self.containers if c["state"] != "running"]

    @property
    def stopped_vpn_containers(self) -> list[str]:
        """Stopped containers clients connect to: a stopped probe leaves the VPN working."""
        return [name for name in self.stopped_containers if name != PROBE_CONTAINER]

    @property
    def awg3_dropped(self) -> bool:
        """amnezia-awg2 runs without the AmneziaWG 3.1 settings: its kernel module is older."""
        return any(c.get("awg3_dropped") for c in self.containers)

    @property
    def missing_interfaces(self) -> list[str]:
        """Running WireGuard-family containers whose interface is gone, e.g. killed for memory."""
        return [c["name"] for c in self.containers if c.get("interface_missing")]

    @property
    def probe_running(self) -> bool:
        return any(
            c["name"] == PROBE_CONTAINER and c["state"] == "running" for c in self.containers
        )

    def used_percent(self, resource: "LoadResource") -> float | None:
        """How busy `resource` was in this reading; None when it was not read."""
        if resource == LoadResource.CPU:
            return self.cpu_percent
        if resource == LoadResource.MEMORY:
            return self.memory_percent
        return self.disk_percent


class LoadResource(models.TextChoices):
    CPU = "cpu", _("CPU")
    MEMORY = "memory", _("Memory")
    DISK = "disk", _("Disk")


class LoadAlert(models.Model):
    """The high load of one resource of a server and when administrators last heard of it.

    `high_since`: the first reading of the run above the threshold; None while the load is
    normal. `told_at` outlives the run, so a load back above the threshold soon after is not
    told again (apps/servers/services/load.py).
    """

    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="load_alerts")
    resource = models.CharField(_("resource"), max_length=8, choices=LoadResource)
    high_since = models.DateTimeField(_("high since"), null=True, blank=True)
    told_at = models.DateTimeField(_("told at"), null=True, blank=True)

    class Meta:
        verbose_name = _("load alert")
        verbose_name_plural = _("load alerts")
        constraints = (
            models.UniqueConstraint(
                fields=["server", "resource"], name="servers_loadalert_unique_resource"
            ),
        )

    def __str__(self) -> str:
        return f"{self.server_id} {self.resource}"


class InterfaceRepair(models.Model):
    """A running WireGuard-family container that lost its interface and its restarts so far.

    The row lives from the run of readings that found the interface gone until a reading finds it
    back or the container stopped (apps/servers/services/interface_watch.py).
    """

    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="interface_repairs")
    container = models.CharField(_("container"), max_length=64)
    missing_since = models.DateTimeField(_("missing since"))
    attempts = models.PositiveSmallIntegerField(_("restarts"), default=0)
    next_attempt_at = models.DateTimeField(_("next restart at"), null=True, blank=True)
    gave_up = models.BooleanField(_("gave up"), default=False)

    class Meta:
        verbose_name = _("interface repair")
        verbose_name_plural = _("interface repairs")
        constraints = (
            models.UniqueConstraint(
                fields=["server", "container"], name="servers_interfacerepair_unique_container"
            ),
        )

    def __str__(self) -> str:
        return f"{self.server_id} {self.container}"


class ServerAddress(models.Model):
    """Another IP or domain of a server that keys may send clients to instead of its own address.

    Keys may take it only once the check found the SSH key the panel trusts for the server at
    this address: a second IP at the hosting provider, or a domain on one of its IPs.
    """

    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="addresses")
    host = models.CharField(_("address"), max_length=255)
    verified_at = models.DateTimeField(_("checked at"), null=True, blank=True)
    # Why the latest check failed; empty after a successful one.
    error = models.CharField(_("check error"), max_length=500, blank=True)
    created_at = models.DateTimeField(_("added at"), auto_now_add=True)

    class Meta:
        verbose_name = _("server address")
        verbose_name_plural = _("server addresses")
        ordering = ("host",)
        constraints = (
            models.UniqueConstraint(
                fields=["server", "host"],
                name="servers_address_unique_host",
                violation_error_message=_("The server already has this address."),
            ),
        )

    def __str__(self) -> str:
        return self.host

    @property
    def verified(self) -> bool:
        return self.verified_at is not None

    def record_check(self, error: str = "") -> None:
        """A check passed without `error`; a failed one leaves the address unusable."""
        self.verified_at = None if error else timezone.now()
        self.error = error[:500]
        self.save(update_fields=["verified_at", "error"])


def _used_percent(total: int | None, free: int | None) -> float | None:
    if not total or free is None:
        return None
    return round(100 * (total - free) / total, 1)
