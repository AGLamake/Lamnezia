"""The check a restore makes of a container already on the server, before the installer starts it.

A container left by an earlier attempt or by the Amnezia app is kept when it holds the keys
the panel has; one with other keys is refused before it is started, configured or seeded.
"""

from collections.abc import Callable

from django.utils.translation import gettext as _

from amnezia_core.errors import AmneziaFormatError
from apps.backups.errors import RestoreError
from apps.operations.services.context import OperationContext
from apps.protocols.services.container import ProtocolContainer


class IdentityGuard:
    """Compares what the identity file of the container names with what the panel keeps.

    `identity` reads the file: the server private key, the CA fingerprint, the onion address
    or the proxy secret. A container without the file was never configured: the restore
    fills it in. A container with other keys is refused with `refusal`.
    """

    def __init__(
        self,
        ctx: OperationContext,
        *,
        path: str,
        identity: Callable[[str], str],
        expected: str,
        refusal: RestoreError,
    ) -> None:
        self._ctx = ctx
        self._path = path
        self._identity = identity
        self._expected = expected
        self._refusal = refusal

    def check(self, docker: ProtocolContainer) -> None:
        content = docker.copy_file(self._path)
        if content is None:
            self._ctx.log(_("The container has no files of its own yet."))
            return
        if self._read(content.decode(errors="replace")) != self._expected:
            raise self._refusal
        self._ctx.log(_("The container has the keys the panel keeps."))

    def _read(self, text: str) -> str:
        try:
            return self._identity(text)
        except AmneziaFormatError:
            return ""  # a file the panel cannot read is not the one it wrote
