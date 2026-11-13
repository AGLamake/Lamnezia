from typing import TYPE_CHECKING

from django.db import models
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg_export import AWG3_VERSION, awg_version
from amnezia_core.containers import AWG2, WG_CONTAINERS
from amnezia_core.proxies import SOCKS5_CONTAINER, ProxyLinks, proxy_links
from amnezia_core.services import DNS_CONTAINER, SFTP_CONTAINER, TOR_CONTAINER
from amnezia_core.vpn_link import DEFAULT_DNS
from apps.protocols.catalog import CATALOG, ContainerGroup, titles
from apps.ssh.encryption import EncryptedTextField

if TYPE_CHECKING:
    from apps.servers.models import Server

# What keys and traffic call an instance: "instance:<id>".
_SOURCE_PREFIX = "instance:"
# The `settings` key of SOCKS5 with the size of the 3proxy.log the app left, in bytes.
APP_LOG_SETTING = "app_log"
# The `settings` key of AmneziaDNS whose config does not turn the query logs off: built by the app.
DNS_LOGS_SETTING = "query_logs_unset"

# Names the panel shows for the protocol and the service containers it manages.
CONTAINER_TITLES = titles(ContainerGroup.VPN)
SERVICE_TITLES = titles(ContainerGroup.SERVICES)


class InstanceOrigin(models.TextChoices):
    IMPORTED = "imported", _("imported")
    INSTALLED = "installed", _("installed by the panel")


class ProtocolInstance(models.Model):
    """A protocol container on a server, e.g. amnezia-awg2, with its settings at the last read.

    The settings are a snapshot for the interface: every change on the server re-reads the files
    (DESIGN.md 5.4). `source` is the id keys and traffic use for the instance.
    """

    server = models.ForeignKey(
        "servers.Server",
        verbose_name=_("server"),
        on_delete=models.CASCADE,
        related_name="instances",
    )
    container = models.CharField(_("container"), max_length=64)
    port = models.PositiveIntegerField(_("port"))
    subnet = models.CharField(_("subnet"), max_length=43)
    params = models.JSONField(_("protocol parameters"), default=dict, blank=True)
    # What client profiles get; the server itself does not use it.
    primary_dns = models.GenericIPAddressField(_("DNS"), protocol="IPv4", default=DEFAULT_DNS[0])
    secondary_dns = models.GenericIPAddressField(
        _("secondary DNS"), protocol="IPv4", default=DEFAULT_DNS[1]
    )
    public_key = models.CharField(_("server public key"), max_length=64)
    private_key = EncryptedTextField(_("server private key"))
    preshared_key = EncryptedTextField(_("preshared key"), blank=True)
    origin = models.CharField(_("origin"), max_length=16, choices=InstanceOrigin)
    # Docker keeps the container log; Amnezia itself starts containers without one.
    container_logs = models.BooleanField(_("keep the container log"), default=False)
    # The container is removed from the server, the keys and the backup are kept for a restore.
    container_removed_at = models.DateTimeField(_("container removed at"), null=True, blank=True)
    created_at = models.DateTimeField(_("added at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("read at"), auto_now=True)

    class Meta:
        verbose_name = _("protocol instance")
        verbose_name_plural = _("protocol instances")
        ordering = ("server", "container")
        constraints = (
            models.UniqueConstraint(
                fields=["server", "container"], name="protocols_instance_unique_container"
            ),
        )

    def __str__(self) -> str:
        return f"{self.server} · {self.title}"

    @property
    def title(self) -> str:
        return str(CONTAINER_TITLES.get(self.container, self.container))

    @property
    def description(self) -> str:
        kind = CATALOG.get(self.container)
        return str(kind.description) if kind else ""

    @property
    def awg_version(self) -> str:
        """The AmneziaWG version of the parameters ("3.1", "2", "1.5", "1.0"); empty if not AWG."""
        kind = WG_CONTAINERS.get(self.container)
        return awg_version(kind, self.params) if kind else ""

    @property
    def awg_outdated(self) -> bool:
        """AmneziaWG 2.0 below 3.1: the Amnezia app warns its administrator to move to 3.1."""
        return self.container == AWG2.name and self.awg_version != AWG3_VERSION

    @property
    def dns(self) -> tuple[str, str]:
        return self.primary_dns, self.secondary_dns

    @property
    def removed(self) -> bool:
        return self.container_removed_at is not None

    @property
    def source(self) -> str:
        return f"{_SOURCE_PREFIX}{self.pk}"

    @staticmethod
    def pk_of(source: str) -> int | None:
        """The id of the instance a `source` names; None for a source that is not an instance."""
        prefix, _sep, pk = source.partition(":")
        return int(pk) if f"{prefix}:" == _SOURCE_PREFIX and pk.isdigit() else None

    @classmethod
    def sources_of(cls, server: "Server") -> list[str]:
        """Sources of the server's instances; loads only ids, so no secret is decrypted."""
        return [instance.source for instance in cls.objects.filter(server=server).only("pk")]


class ServiceInstance(models.Model):
    """An Amnezia service container on a server, with what the app shows for it.

    `secret` is the SFTP or SOCKS5 password, the onion address of the Tor site or the secret
    of a Telegram proxy, read from the container by the import; it is stored encrypted.
    `settings` keeps what a Telegram proxy needs for its links besides the secret: the transport
    mode, the TLS domain and the public host, or the domain of TProxy. For SOCKS5 it keeps the
    size of the 3proxy.log the app left (`APP_LOG_SETTING`), which the panel offers to delete;
    for AmneziaDNS, that its config leaves the query logs as the image has them
    (`DNS_LOGS_SETTING`), and the panel offers to rebuild it.
    """

    server = models.ForeignKey(
        "servers.Server",
        verbose_name=_("server"),
        on_delete=models.CASCADE,
        related_name="services",
    )
    container = models.CharField(_("container"), max_length=64)
    port = models.PositiveIntegerField(_("port"), null=True, blank=True)
    login = models.CharField(_("login"), max_length=64, blank=True)
    secret = EncryptedTextField(_("password or address"), blank=True)
    settings = models.JSONField(_("settings"), default=dict, blank=True)
    origin = models.CharField(_("origin"), max_length=16, choices=InstanceOrigin)
    created_at = models.DateTimeField(_("added at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("read at"), auto_now=True)

    class Meta:
        verbose_name = _("service")
        verbose_name_plural = _("services")
        ordering = ("server", "container")
        constraints = (
            models.UniqueConstraint(
                fields=["server", "container"], name="protocols_service_unique_container"
            ),
        )

    def __str__(self) -> str:
        return f"{self.server} · {self.title}"

    @property
    def title(self) -> str:
        return str(SERVICE_TITLES.get(self.container, self.container))

    @property
    def description(self) -> str:
        kind = CATALOG.get(self.container)
        return str(kind.description) if kind else ""

    @property
    def is_sftp(self) -> bool:
        return self.container == SFTP_CONTAINER

    @property
    def is_tor_site(self) -> bool:
        return self.container == TOR_CONTAINER

    @property
    def is_socks5(self) -> bool:
        return self.container == SOCKS5_CONTAINER

    @property
    def is_dns(self) -> bool:
        return self.container == DNS_CONTAINER

    @property
    def dns_logs_unset(self) -> bool:
        """AmneziaDNS built by the app: its config does not turn the query logs off."""
        return self.is_dns and bool(self.settings.get(DNS_LOGS_SETTING))

    @property
    def app_log_size(self) -> int | None:
        """Bytes of the 3proxy.log of hosts users visited, left by the app; None without one."""
        size = self.settings.get(APP_LOG_SETTING) if self.is_socks5 else None
        return int(size) if size is not None else None

    @property
    def links(self) -> ProxyLinks | None:
        """tg:// and t.me links of a Telegram proxy; they carry the secret."""
        return proxy_links(
            self.container,
            secret=self.secret,
            port=self.port,
            server_host=self.server.host,
            settings=self.settings,
        )


class AwgMigration(models.Model):
    """The move of the clients of a legacy AmneziaWG container to amnezia-awg2 on 3.1.

    `pairs` are [old key id, new key id]: every client gets a new key on the new container while
    the old one keeps working. `moved_at` is set when all of them are issued; the legacy container
    is removed only after that, when the administrator confirms.
    """

    legacy = models.OneToOneField(
        ProtocolInstance,
        verbose_name=_("legacy AmneziaWG"),
        on_delete=models.CASCADE,
        related_name="migration",
    )
    pairs = models.JSONField(_("moved keys"), default=list, blank=True)
    created_at = models.DateTimeField(_("started at"), auto_now_add=True)
    moved_at = models.DateTimeField(_("keys moved at"), null=True, blank=True)

    class Meta:
        verbose_name = _("move to AmneziaWG 3.1")
        verbose_name_plural = _("moves to AmneziaWG 3.1")

    def __str__(self) -> str:
        return f"{self.legacy} → AmneziaWG 3.1"

    @property
    def old_key_ids(self) -> set[int]:
        return {old for old, _new in self.pairs}


class OffloadWorkaround(models.Model):
    """TSO/GSO of the WireGuard-family containers of a server, as the panel last read them.

    `enabled` says whether the systemd timer of the workaround runs on the server
    (services/offload_scripts.py); `interfaces` is the latest reading of the containers.
    """

    server = models.OneToOneField(
        "servers.Server",
        verbose_name=_("server"),
        on_delete=models.CASCADE,
        related_name="offload",
    )
    enabled = models.BooleanField(_("TSO/GSO workaround on"), default=False)
    # [{"container", "interface", "tso", "gso"}]: InterfaceOffload.to_json().
    interfaces = models.JSONField(_("container interfaces"), default=list, blank=True)
    checked_at = models.DateTimeField(_("checked at"), null=True, blank=True)

    class Meta:
        verbose_name = _("TSO/GSO workaround")
        verbose_name_plural = _("TSO/GSO workarounds")

    def __str__(self) -> str:
        return f"{self.server} · TSO/GSO"
