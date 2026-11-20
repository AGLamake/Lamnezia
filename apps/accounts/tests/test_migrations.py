from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.accounts.models import Management, User
from apps.accounts.services.zone import Zone
from apps.support.services.inbox import Inbox

RELEASE_1_0 = [("accounts", "0010_sign_in_alerts"), ("plans", "0004_max_devices")]
"""The last migrations before several staff members per user; the schema of `manager` is that
of 1.0.0."""


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    executor.migrate(RELEASE_1_0)
    executor.loader.build_graph()
    yield executor
    executor.loader.build_graph()
    executor.migrate(leaves)


def before(executor: MigrationExecutor) -> list[tuple[str, str]]:
    """Every app at its leaf, but accounts and plans as they were before the change."""
    moved = {app for app, _ in RELEASE_1_0}
    leaves = executor.loader.graph.leaf_nodes()
    return [node for node in leaves if node[0] not in moved] + RELEASE_1_0


@pytest.mark.django_db(transaction=True)
def test_the_moderators_of_1_0_keep_their_users_servers_quota_and_requests(
    executor: MigrationExecutor,
) -> None:
    old = executor.loader.project_state(before(executor)).apps
    users = old.get_model("accounts", "User")
    servers = old.get_model("servers", "Server")
    conversations = old.get_model("support", "Conversation")
    anna = users.objects.create(username="anna", role="moderator", user_quota=2)
    boris = users.objects.create(username="boris", role="moderator")
    served = [users.objects.create(username=f"u{n}", manager=anna) for n in range(2)]
    users.objects.create(username="loner")
    users.objects.create(username="borisa", manager=boris)
    anna.moderated_servers.add(servers.objects.create(name="Amsterdam", host="203.0.113.7"))
    request = conversations.objects.create(user=served[0], kind="ticket")

    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())

    moderator = User.objects.get(username="anna")
    assert set(Zone(moderator).users().values_list("username", flat=True)) == {"u0", "u1"}
    assert list(moderator.moderated_servers.values_list("name", flat=True)) == ["Amsterdam"]
    assert moderator.user_quota == 2
    assert moderator.is_over_user_quota()
    assert [c.pk for c in Inbox(moderator).requests()] == [request.pk]
    links = Management.objects.order_by("user__username")
    assert [(m.user.username, m.member.username, m.via_plan) for m in links] == [
        ("borisa", "boris", False),
        ("u0", "anna", False),
        ("u1", "anna", False),
    ]
