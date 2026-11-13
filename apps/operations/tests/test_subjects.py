import pytest

from apps.operations.services.subjects import SubjectLinks, subjects
from apps.operations.tests.factories import OperationFactory
from apps.protocols.models import ProtocolInstance
from apps.servers.tests.factories import ServerFactory


def test_the_most_specific_parameter_wins() -> None:
    links = SubjectLinks()
    links.register("server_id", lambda pk: f"/servers/{pk}/")
    links.register("key_id", lambda pk: f"/keys/{pk}/", priority=10)

    operation = OperationFactory.build(params={"server_id": 1, "key_id": 2})

    assert links.url(operation) == "/keys/2/"


def test_an_operation_without_a_known_id_has_no_link() -> None:
    assert subjects.url(OperationFactory.build(params={"container": "amnezia-awg2"})) is None


@pytest.mark.django_db
def test_apps_link_their_operations_to_pages() -> None:
    server = ServerFactory(containers=[])
    instance = ProtocolInstance.objects.create(
        server=server, container="amnezia-awg2", port=443, subnet="10.8.1.0/24"
    )

    urls = [
        subjects.url(OperationFactory.build(params=params))
        for params in ({"server_id": server.pk}, {"key_id": 7}, {"instance_id": instance.pk})
    ]

    assert urls == [f"/servers/{server.pk}/", "/keys/7/", f"/servers/{server.pk}/"]


@pytest.mark.django_db
def test_a_removed_protocol_leaves_no_link() -> None:
    assert subjects.url(OperationFactory.build(params={"instance_id": 999_999})) is None
