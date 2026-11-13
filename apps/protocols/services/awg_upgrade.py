"""Start of the move of an AmneziaWG 2.0 server to 3.1, and which instances can make it.

The Amnezia app gets 3.1 only by a reinstall that loses every client. The panel keeps the server
keys and clientsTable (AwgReconfiguration.upgrade); clients import their keys again. The new
parameters, the header protection key among them, are made by the operation itself: neither its
params nor the audit carry them.
"""

from amnezia_core.awg_export import has_awg3_params
from amnezia_core.containers import AWG2
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance

UPGRADE_KIND = "protocols.upgrade_awg"


def can_upgrade(instance: ProtocolInstance) -> bool:
    """An AmneziaWG 2.0 container that is still on the server."""
    return (
        instance.container == AWG2.name
        and not instance.removed
        and not has_awg3_params(instance.params)
    )


def start_awg_upgrade(
    instance: ProtocolInstance, user: User, *, reissue: bool, ip: str | None = None
) -> Operation:
    """`reissue`: imported keys, which have no profile to update, are issued anew."""
    params = {"instance_id": instance.pk, "user_id": user.pk, "reissue": reissue}
    operation = launch(UPGRADE_KIND, params, user=user, subject=str(instance))
    audit.log(UPGRADE_KIND, actor=user, target=instance, data={"operation": operation.pk}, ip=ip)
    return operation
