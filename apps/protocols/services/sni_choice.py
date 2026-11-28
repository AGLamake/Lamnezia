"""Choice of the Reality SNI of XRay: warnings about a widely used SNI or one another server uses.

A domain that many Reality servers share, the Amnezia default among them, singles a server out
by itself; one SNI on several servers of the panel ties them together, so a block found on one
reaches the others. The list of widely used SNI is the ones guides and defaults hand out; it is
not complete.
"""

from collections import defaultdict
from dataclasses import dataclass

from amnezia_core.xray import DEFAULT_SITE, XRAY_CONTAINER
from apps.protocols.models import ProtocolInstance, SniScan
from apps.protocols.services.sni_scan import SniCandidate, candidates_of
from apps.protocols.services.xray_settings import XrayPanelSettings
from apps.servers.models import Server

MASS_SNIS = frozenset(
    {
        DEFAULT_SITE,
        "addons.mozilla.org",
        "discord.com",
        "dl.google.com",
        "github.com",
        "www.amazon.com",
        "www.apple.com",
        "www.cloudflare.com",
        "www.google.com",
        "www.icloud.com",
        "www.lovelive-anime.jp",
        "www.microsoft.com",
        "www.nvidia.com",
        "www.samsung.com",
        "www.speedtest.net",
        "www.yahoo.com",
    }
)


def is_mass_sni(sni: str) -> bool:
    """Many Reality servers use this SNI: a guide or a default hands it out."""
    return sni.strip().lower() in MASS_SNIS


@dataclass(frozen=True)
class SniOption:
    """A domain the scan found, with the other servers of the panel whose XRay uses it."""

    candidate: SniCandidate
    shared_with: list[str]

    @property
    def mass(self) -> bool:
        return is_mass_sni(self.candidate.domain)


class SniReuse:
    """The SNI of the XRay protocols of the panel on servers other than `server`."""

    def __init__(self, server: Server) -> None:
        instances = ProtocolInstance.objects.filter(
            container=XRAY_CONTAINER, container_removed_at=None
        ).exclude(server=server)
        servers: dict[str, set[str]] = defaultdict(set)
        for instance in instances.select_related("server"):
            sni = XrayPanelSettings.of(instance).server.sni.lower()
            servers[sni].add(instance.server.name)
        self._servers = servers

    def servers(self, sni: str) -> list[str]:
        """Names of the other servers whose XRay uses `sni`, in order."""
        return sorted(self._servers.get(sni.strip().lower(), ()))

    def options(self, candidates: list[SniCandidate]) -> list[SniOption]:
        return [SniOption(candidate, self.servers(candidate.domain)) for candidate in candidates]


class SniAdvice:
    """What the settings page of an XRay protocol says about its SNI: the warnings about the
    current one and the domains of the latest scan of the neighbours."""

    def __init__(self, instance: ProtocolInstance) -> None:
        reuse = SniReuse(instance.server)
        self.sni = XrayPanelSettings.of(instance).server.sni
        self.mass = is_mass_sni(self.sni)
        self.shared_with = reuse.servers(self.sni)
        self.scan = SniScan.objects.filter(server=instance.server).first()
        self.options = reuse.options(candidates_of(instance.server))

    @property
    def domains(self) -> list[str]:
        return [option.candidate.domain for option in self.options]
