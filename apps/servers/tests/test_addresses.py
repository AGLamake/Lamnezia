import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.servers.forms import ServerAddressForm
from apps.servers.models import Server, ServerAddress
from apps.servers.services.addresses import (
    ADD_ADDRESS,
    CHECK_ADDRESS_KIND,
    REMOVE_ADDRESS,
    AddressCheck,
    AddressError,
    AddressErrorCode,
)
from apps.servers.tests.factories import ServerFactory
from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.models import KnownHost

pytestmark = pytest.mark.django_db

KEY = ("ssh-ed25519", "SHA256:nThbg6kXUpJWGl7E1IGOCspRomTxdCARLviKw6E5SY8")
SECOND = "198.51.100.7"


@pytest.fixture
def server() -> Server:
    server = ServerFactory(host="203.0.113.10", port=22)
    KnownHost.objects.create(host=server.host, port=22, key_type=KEY[0], fingerprint=KEY[1])
    return server


@pytest.fixture
def address(server: Server) -> ServerAddress:
    return ServerAddress.objects.create(server=server, host=SECOND)


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def check(address: ServerAddress, key: tuple[str, str]) -> list[tuple[str, int]]:
    """Run the check against a machine showing `key`; returns where it looked."""
    seen: list[tuple[str, int]] = []

    def read(host: str, port: int, timeout: float) -> tuple[str, str]:
        seen.append((host, port))
        return key

    ctx = OperationContext(OperationFactory())
    with ctx.step("check"):
        AddressCheck(address, ctx, read).run()
    return seen


def test_an_address_showing_the_server_key_leads_to_it(address: ServerAddress) -> None:
    assert check(address, KEY) == [(SECOND, 22)]

    address.refresh_from_db()
    assert address.verified
    assert address.error == ""


def test_an_address_of_another_machine_fails_the_check(address: ServerAddress) -> None:
    with pytest.raises(AddressError) as caught:
        check(address, ("ssh-ed25519", "SHA256:other"))

    address.refresh_from_db()
    assert caught.value.code == AddressErrorCode.OTHER_MACHINE
    assert not address.verified
    assert address.error == caught.value.message


def test_a_checked_address_that_stops_answering_is_no_longer_trusted(
    address: ServerAddress,
) -> None:
    address.record_check()

    def unreachable(host: str, port: int, timeout: float) -> tuple[str, str]:
        raise SshConnectionError(ErrorCode.HOST_UNREACHABLE)

    ctx = OperationContext(OperationFactory())
    with pytest.raises(SshConnectionError), ctx.step("check"):
        AddressCheck(address, ctx, unreachable).run()

    address.refresh_from_db()
    assert not address.verified
    assert address.error


def test_without_the_server_key_there_is_nothing_to_compare(address: ServerAddress) -> None:
    KnownHost.objects.all().delete()

    with pytest.raises(AddressError) as caught:
        check(address, KEY)

    assert caught.value.code == AddressErrorCode.NO_TRUSTED_KEY


@pytest.mark.parametrize(
    ("host", "cleaned"),
    [(" VPN2.Example.com ", "vpn2.example.com"), ("[2001:db8::1]", "2001:db8::1")],
)
def test_the_form_tidies_the_address(server: Server, host: str, cleaned: str) -> None:
    form = ServerAddressForm({"host": host}, server=server)

    assert form.is_valid(), form.errors
    assert form.cleaned_data["host"] == cleaned


@pytest.mark.parametrize("host", ["203.0.113.10", SECOND, "not a host!"])
def test_the_form_refuses_own_repeated_and_broken_addresses(
    address: ServerAddress, host: str
) -> None:
    assert not ServerAddressForm({"host": host}, server=address.server).is_valid()


def test_adding_an_address_starts_its_check(admin_client: Client, server: Server) -> None:
    response = admin_client.post(f"/servers/{server.pk}/addresses/", {"host": SECOND})

    operation = Operation.objects.get(kind=CHECK_ADDRESS_KIND)
    address = ServerAddress.objects.get(server=server)
    assert response["Location"] == f"/operations/{operation.pk}/"
    assert operation.params == {"server_id": server.pk, "address_id": address.pk}
    assert AuditEvent.objects.get(action=ADD_ADDRESS).data == {"host": SECOND}
    assert SECOND in admin_client.get(f"/servers/{server.pk}/").content.decode()


def test_an_address_can_be_checked_again(admin_client: Client, address: ServerAddress) -> None:
    server = address.server

    admin_client.post(f"/servers/{server.pk}/addresses/{address.pk}/check/")

    assert Operation.objects.filter(kind=CHECK_ADDRESS_KIND).count() == 1


def test_removing_an_address_without_keys_goes_back_to_the_card(
    admin_client: Client, address: ServerAddress
) -> None:
    server = address.server

    response = admin_client.post(f"/servers/{server.pk}/addresses/{address.pk}/remove/")

    assert response["Location"] == f"/servers/{server.pk}/"
    assert not ServerAddress.objects.exists()
    assert AuditEvent.objects.get(action=REMOVE_ADDRESS).data == {"host": SECOND}


def test_a_guest_cannot_add_addresses(client: Client, server: Server) -> None:
    client.force_login(UserFactory())

    client.post(f"/servers/{server.pk}/addresses/", {"host": SECOND})

    assert not ServerAddress.objects.exists()
