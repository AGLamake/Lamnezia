"""Start of a change of the protocol settings: the background operation and the audit entry."""

from typing import Any, ClassVar, Protocol, Self

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance


class PanelSettings(Protocol):
    """Settings of one protocol as the operation `kind` gets them in its params."""

    kind: ClassVar[str]

    def to_params(self) -> dict[str, Any]: ...


class ContainerSettings(PanelSettings, Protocol):
    """Settings stored on the instance and in every key profile, compared with the current."""

    def touches_server(self, other: Self) -> bool:
        """The server or the container must change."""
        ...

    def changes_profiles(self, other: Self) -> bool:
        """Clients must import their keys again."""
        ...

    def store(self, instance: ProtocolInstance) -> None:
        """Save the settings on the instance."""
        ...

    def updated_profile(self, text: str) -> str:
        """A stored key profile with the settings."""
        ...


def start_settings_change(
    instance: ProtocolInstance, settings: PanelSettings, user: User, *, reissue: bool
) -> Operation:
    """`reissue`: the keys imported from the server get new ones after the change."""
    params = {
        "instance_id": instance.pk,
        "user_id": user.pk,
        "reissue": reissue,
        **settings.to_params(),
    }
    operation = launch(settings.kind, params, user=user, subject=str(instance))
    audit.log(settings.kind, actor=user, target=instance, data=params)
    return operation
