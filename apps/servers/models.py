from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

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
    def probe_running(self) -> bool:
        return any(
            c["name"] == PROBE_CONTAINER and c["state"] == "running" for c in self.containers
        )


def _used_percent(total: int | None, free: int | None) -> float | None:
    if not total or free is None:
        return None
    return round(100 * (total - free) / total, 1)
