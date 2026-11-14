"""Key changes of a server group: made on every member that answers; the others catch up later.

The keys of a group (docs/FALLBACK.md section 4) belong to its leading server, and the members
are its clones: each of them must have the same peers. The app that owns groups registers a
lookup in `key_groups`, so keys do not depend on it.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from typing import Protocol

from django.utils.translation import gettext as _

from apps.keys.errors import PeerError, PeerErrorCode
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.backends import KeyBackend, Reissue, ReissueOutcome, backend_for
from apps.keys.services.instances import Instance
from apps.keys.services.peers import StepRecorder
from apps.ssh.errors import CatalogError, SshError
from apps.ssh.services import RemoteHost


@dataclass(frozen=True)
class GroupServer:
    """A member server a key change goes to."""

    name: str
    connect: Callable[[], AbstractContextManager[RemoteHost]]


class KeyGroup(Protocol):
    """The members of the group a key belongs to."""

    def members(self) -> Sequence[GroupServer]:
        """Members up to date with the group, the leading server first."""
        ...

    def fall_behind(self, server: GroupServer) -> None:
        """Mark the member as missing a change; a catch-up gives it the group files later."""
        ...

    def finish(self) -> None:
        """The change is over: a member that joined or caught up meanwhile may have missed it."""
        ...


class KeyGroups:
    """Usage: ``@key_groups.register`` on a function giving the group of a key source or None."""

    def __init__(self) -> None:
        self._lookup: Callable[[str], KeyGroup | None] | None = None

    def register(
        self, lookup: Callable[[str], KeyGroup | None]
    ) -> Callable[[str], KeyGroup | None]:
        self._lookup = lookup
        return lookup

    def of(self, source: str) -> KeyGroup | None:
        return None if self._lookup is None else self._lookup(source)


key_groups = KeyGroups()


class GroupKeys:
    """A `KeyBackend` that makes a change on the first member that answers, then on the others.

    The first one must succeed: it gives a new key its address, which the others take as is.
    A member that cannot be reached or fails is marked lagging and the change goes on without
    it. Each member is changed by the safe write protocol of its own backend.
    """

    def __init__(
        self,
        primary: tuple[GroupServer, KeyBackend],
        replicas: Sequence[tuple[GroupServer, KeyBackend]],
        group: KeyGroup,
        steps: StepRecorder,
    ) -> None:
        self._primary = primary
        self._replicas = replicas
        self._group = group
        self._steps = steps

    @classmethod
    @contextmanager
    def connect(
        cls, instance: Instance, group: KeyGroup, steps: StepRecorder
    ) -> Iterator["GroupKeys"]:
        """Connect to every member; with none reachable nothing changes and nobody lags."""
        with ExitStack() as stack:
            reached: list[tuple[GroupServer, KeyBackend]] = []
            missed: list[tuple[GroupServer, str]] = []
            with steps.step(_("Connect to the servers of the group")):
                for server in group.members():
                    try:
                        host = stack.enter_context(server.connect())
                    except SshError as error:
                        missed.append((server, error.message))
                        steps.log(f"{server.name}: {error.message}")
                        continue
                    reached.append((server, backend_for(instance, host, steps)))
                if not reached:
                    raise PeerError(PeerErrorCode.NO_GROUP_SERVER, instance.source)
            backends = cls(reached[0], reached[1:], group, steps)
            for server, reason in missed:
                backends._leave_behind(server, reason)
            yield backends

    def issue(self, key: AccessKey) -> None:
        self._on_primary(lambda backend: backend.issue(key))
        self._on_replicas(lambda backend: backend.enable(key))

    def reissue(self, pairs: Sequence[Reissue]) -> ReissueOutcome:
        outcome: ReissueOutcome = self._on_primary(lambda backend: backend.reissue(pairs))

        def copy(backend: KeyBackend) -> None:
            for pair in outcome.done:
                backend.enable(pair.new)
                if pair.old.status != KeyStatus.REVOKED:
                    backend.revoke(pair.old)

        self._on_replicas(copy)
        return outcome

    def disable(self, key: AccessKey) -> None:
        self._on_all(lambda backend: backend.disable(key))

    def enable(self, key: AccessKey) -> None:
        self._on_all(lambda backend: backend.enable(key))

    def revoke(self, key: AccessKey) -> None:
        self._on_all(lambda backend: backend.revoke(key))

    def rename(self, key: AccessKey) -> None:
        self._on_all(lambda backend: backend.rename(key))

    def assign(self, key: AccessKey) -> None:
        # A member puts the peer back with its address in the group, never a new one.
        self._on_primary(lambda backend: backend.assign(key))
        self._on_replicas(lambda backend: backend.enable(key))

    def _on_all(self, change: Callable[[KeyBackend], None]) -> None:
        self._on_primary(change)
        self._on_replicas(change)

    def _on_primary[T](self, change: Callable[[KeyBackend], T]) -> T:
        server, backend = self._primary
        with self._steps.step(_("Change the keys on %(server)s") % {"server": server.name}):
            return change(backend)

    def _on_replicas(self, change: Callable[[KeyBackend], None]) -> None:
        for server, backend in self._replicas:
            try:
                with self._steps.step(_("Copy the change to %(server)s") % {"server": server.name}):
                    change(backend)
            except CatalogError as error:
                self._leave_behind(server, error.message)

    def _leave_behind(self, server: GroupServer, reason: str) -> None:
        with self._steps.step(_("Leave %(server)s to catch up later") % {"server": server.name}):
            self._steps.log(reason)
            self._group.fall_behind(server)
