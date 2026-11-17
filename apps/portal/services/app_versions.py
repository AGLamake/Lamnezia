"""The app versions a key of an AmneziaWG 3.1 server needs, and which keys need them.

Older Amnezia VPN and AmneziaWG apps reject the 3.1 lines of the config, so the key does not
import or does not connect. The versions are the first releases with 3.1 support
(docs/wiki «Protocols», section «AmneziaWG 3.1: which clients fit»).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from amnezia_core.awg_export import AWG3_VERSION
from amnezia_core.containers import AWG2
from apps.keys.models import AccessKey
from apps.protocols.models import ProtocolInstance

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise


@dataclass(frozen=True)
class AppVersion:
    """The first release of an app on these platforms that reads AmneziaWG 3.1 keys."""

    app: str
    platforms: "StrOrPromise"
    version: str


AWG3_APPS = (
    AppVersion("Amnezia VPN", gettext_lazy("Windows, macOS, Linux, Android"), "5.0.1.5"),
    AppVersion("Amnezia VPN", "iOS", "5.0.2"),
    AppVersion("AmneziaWG", "Android", "3.1.20260814"),
    AppVersion("AmneziaWG", "Windows", "3.1.0"),
    AppVersion("AmneziaWG", "iOS", "3.1.4"),
)


def awg3_sources(keys: Iterable[AccessKey]) -> set[str]:
    """Sources of these keys whose instance is AmneziaWG 3.1."""
    pks = {pk for key in keys if (pk := ProtocolInstance.pk_of(key.source)) is not None}
    candidates = ProtocolInstance.objects.filter(pk__in=pks, container=AWG2.name)
    return {
        i.source
        for i in candidates.only("pk", "container", "params")
        if i.awg_version == AWG3_VERSION
    }


def needs_awg3_app(key: AccessKey) -> bool:
    return key.source in awg3_sources([key])


def update_hint() -> str:
    """One paragraph for the bot: update the app to at least these versions."""
    versions = "\n".join(f"• {app.app} ({app.platforms}): {app.version}" for app in AWG3_APPS)
    return _("The key is for AmneziaWG 3.1. Update the app to at least:") + "\n" + versions
