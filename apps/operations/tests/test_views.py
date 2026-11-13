import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.operations.models import Operation, OperationStatus, StepStatus
from apps.operations.tests.conftest import Register
from apps.operations.tests.factories import (
    FailedOperationFactory,
    OperationFactory,
    OperationStepFactory,
)
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_list_shows_recent_operations(admin_client: Client) -> None:
    OperationFactory(subject="Amsterdam")

    html = admin_client.get("/operations/").content.decode()

    assert "Amsterdam" in html
    assert "test.noop" in html


def test_list_filters_by_status_and_subject(admin_client: Client) -> None:
    OperationFactory(subject="Amsterdam", status=OperationStatus.SUCCEEDED)
    OperationFactory(subject="Berlin", status=OperationStatus.SUCCEEDED)
    FailedOperationFactory(subject="Amsterdam two")

    response = admin_client.get("/operations/", {"q": "amster", "status": "succeeded"})

    assert [op.subject for op in response.context["operations"]] == ["Amsterdam"]


def test_list_reloads_itself_while_an_operation_runs(admin_client: Client) -> None:
    OperationFactory(status=OperationStatus.RUNNING)

    assert 'hx-trigger="every' in admin_client.get("/operations/").content.decode()


def test_list_of_finished_operations_stays_still(admin_client: Client) -> None:
    OperationFactory(status=OperationStatus.SUCCEEDED)

    assert 'hx-trigger="every' not in admin_client.get("/operations/").content.decode()


def test_failed_operation_shows_step_message_and_retry_button(admin_client: Client) -> None:
    operation = FailedOperationFactory()
    OperationStepFactory(operation=operation, title="Read config")
    OperationStepFactory(operation=operation, title="Apply config", status=StepStatus.FAILED)

    html = admin_client.get(f"/operations/{operation.pk}/").content.decode()

    assert "Step «Apply config» failed." in html
    assert operation.error_message in html
    assert operation.error_hint in html
    assert f'action="/operations/{operation.pk}/retry/"' in html
    assert f'hx-get="/operations/{operation.pk}/progress/"' not in html


def test_unfinished_operation_polls_its_progress(admin_client: Client) -> None:
    operation = OperationFactory(status=OperationStatus.RUNNING)

    html = admin_client.get(f"/operations/{operation.pk}/progress/").content.decode()

    assert f'hx-get="/operations/{operation.pk}/progress/"' in html
    assert "<html" not in html


def test_retry_redirects_to_the_new_operation(
    admin_client: Client, register_kind: Register
) -> None:
    register_kind("test.retry", lambda ctx: None)
    failed = FailedOperationFactory(kind="test.retry")

    response = admin_client.post(f"/operations/{failed.pk}/retry/")

    retried = Operation.objects.get(retry_of=failed)
    assert response.headers["Location"] == f"/operations/{retried.pk}/"
    assert "Open the retry" in admin_client.get(f"/operations/{failed.pk}/").content.decode()


def test_retry_of_finished_operation_is_refused(admin_client: Client) -> None:
    operation = OperationFactory(status=OperationStatus.SUCCEEDED)

    response = admin_client.post(f"/operations/{operation.pk}/retry/", follow=True)

    assert "Only a failed operation can be retried." in response.content.decode()
    assert Operation.objects.count() == 1


def test_pages_are_for_staff_only(client: Client) -> None:
    operation = OperationFactory()
    client.force_login(UserFactory())

    assert client.get("/operations/").status_code == 403
    assert client.get(f"/operations/{operation.pk}/").status_code == 403
    assert client.post(f"/operations/{operation.pk}/retry/").status_code == 403


def test_operation_leads_to_its_object(admin_client: Client) -> None:
    server = ServerFactory()
    operation = OperationFactory(subject="Amsterdam", params={"server_id": server.pk})

    pages = [admin_client.get(url) for url in ("/operations/", f"/operations/{operation.pk}/")]

    assert all(f'href="/servers/{server.pk}/"' in page.content.decode() for page in pages)
