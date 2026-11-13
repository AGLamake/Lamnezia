from amnezia_core.awg import Peer
from amnezia_core.clients_table import ClientsTable
from amnezia_core.xray import client_fingerprint
from apps.keys.models import AccessKey, KeyStatus

_NAME_LENGTH = AccessKey._meta.get_field("name").max_length or 100


def adopt_peer(
    source: str, peer: Peer, clients: ClientsTable, fallback_name: str
) -> tuple[AccessKey, bool]:
    """Take a peer the panel did not create as an ownerless active key; True if it is new.

    The key has no private half: only the client device has it.
    """
    # A peer added by hand or by an older app may be missing from clientsTable.
    named = clients.name(peer.public_key) if peer.public_key in clients else None
    addresses = peer.addresses
    return AccessKey.objects.get_or_create(
        source=source,
        public_key=peer.public_key,
        defaults={
            "name": (named or fallback_name)[:_NAME_LENGTH],
            "client_ip": str(addresses[0]) if addresses else None,
            "imported": True,
            "status": KeyStatus.ACTIVE,
        },
    )


def adopt_certificate_client(
    source: str, client_id: str, clients: ClientsTable, fallback_name: str
) -> tuple[AccessKey, bool]:
    """Take an OpenVPN or IPsec client certificate as an ownerless active key; True if new.

    The key has no private half: the panel did not make it, so it cannot hand the key out.
    """
    named = clients.name(client_id) if client_id in clients else None
    return AccessKey.objects.get_or_create(
        source=source,
        public_key=client_id,
        defaults={
            "name": (named or fallback_name)[:_NAME_LENGTH],
            "imported": True,
            "status": KeyStatus.ACTIVE,
        },
    )


def adopt_xray_client(
    source: str, client_id: str, clients: ClientsTable, fallback_name: str
) -> tuple[AccessKey, bool]:
    """Take an XRay client as an ownerless active key; True if it is new.

    The UUID is the client's credential, so it is kept only encrypted, as the private key; the
    key is found by its fingerprint.
    """
    named = clients.name(client_id) if client_id in clients else None
    return AccessKey.objects.get_or_create(
        source=source,
        public_key=client_fingerprint(client_id),
        defaults={
            "name": (named or fallback_name)[:_NAME_LENGTH],
            "private_key": client_id,
            "status": KeyStatus.ACTIVE,
        },
    )
