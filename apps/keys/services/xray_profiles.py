"""Profiles of XRay keys kept in line with server.json: built for imported keys, a new shortId.

The panel keeps the server half of a profile with every key. An imported XRay key has only its
UUID, so its profile is built from server.json when the key is first shown. The shortId is part
of every profile: when it changes on the server (e.g. the app set the protocol up anew), the
stored profiles get the new one.
"""

from collections.abc import Iterable
from dataclasses import replace
from datetime import timedelta

from django.utils import timezone

from amnezia_core.xray import (
    XRAY_CONFIG_PATH,
    XRAY_CONTAINER,
    XRAY_PUBLIC_KEY_PATH,
    XrayServerConfig,
)
from apps.keys.errors import explained_format_errors
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.services.profile import XrayKeyProfile, profile_container
from apps.keys.signals import xray_short_id_changed
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.ssh.services import RemoteHost

BUILD_PROFILES = "keys.build_xray_profiles"

_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)
# A build that failed, e.g. the server did not answer, is not started again before this.
_RETRY_AFTER = timedelta(minutes=10)


class XrayProfiles:
    """The profiles of the keys of one XRay instance, read from its server over `host`."""

    def __init__(self, instance: Instance, host: RemoteHost) -> None:
        self._instance = instance
        self._host = host

    def current(self) -> XrayKeyProfile:
        """The profile clients connect with now; a new shortId first updates the stored ones."""
        container = self._host.container(XRAY_CONTAINER)
        config_text = container.read_file(XRAY_CONFIG_PATH).decode()
        public_key = container.read_file(XRAY_PUBLIC_KEY_PATH).decode().strip()
        with explained_format_errors():
            config = XrayServerConfig.parse(config_text)
            server, short_id = config.settings(), config.short_id
        if short_id != self._instance.short_id:
            follow_short_id(self._instance.source, short_id)
        profile = XrayKeyProfile.build(self._instance.host, server, public_key, short_id)
        return replace(profile, dns=self._instance.dns)

    def fill(self) -> int:
        """Give a profile to every key of the instance that waits for one; how many got it."""
        waiting = [
            key
            for key in AccessKey.objects.filter(source=self._instance.source)
            if key.awaits_profile
        ]
        if waiting:
            profile = self.current().to_json()
            for key in waiting:
                key.profile = profile
            AccessKey.objects.bulk_update(waiting, ["profile"])
        return len(waiting)


def follow_short_id(source: str, short_id: str) -> int:
    """Put `short_id` into the stored XRay profiles of `source`; how many profiles changed.

    The instance remembers it through `xray_short_id_changed`.
    """
    changed = []
    for key in AccessKey.objects.filter(source=source).exclude(status=KeyStatus.REVOKED):
        if profile_container(key.profile) != XRAY_CONTAINER:
            continue
        profile = XrayKeyProfile.from_json(key.profile)
        if profile.short_id != short_id:
            key.profile = replace(profile, short_id=short_id).to_json()
            changed.append(key)
    AccessKey.objects.bulk_update(changed, ["profile"])
    xray_short_id_changed.send(XrayProfiles, source=source, short_id=short_id)
    return len(changed)


def request_profiles(keys: Iterable[AccessKey]) -> bool:
    """Start building the profiles `keys` wait for, one operation per instance at a time.

    True while a build for one of them is under way: the page may wait for it.
    """
    xray = {i.source: i.title for i in instances if i.container.name == XRAY_CONTAINER}
    sources = {key.source for key in keys if key.awaits_profile and key.source in xray}
    return any([_request(source, xray[source]) for source in sorted(sources)])


def _request(source: str, title: str) -> bool:
    latest = Operation.objects.filter(kind=BUILD_PROFILES, params__source=source).order_by("-pk")
    operation = latest.first()
    if operation is not None and operation.status in _UNFINISHED:
        return True
    if (
        operation is not None
        and operation.status == OperationStatus.FAILED
        and operation.created_at > timezone.now() - _RETRY_AFTER
    ):
        return False
    launch(BUILD_PROFILES, {"source": source}, subject=title)
    return True
