"""Links between users and the staff of their plan: ways of giving a plan, quota, staff edits."""

import pytest

from apps.accounts.models import Management, Role, User
from apps.accounts.services.letters import Letters
from apps.accounts.services.registration import Applicant, Registration
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure
from apps.audit.models import AuditEvent
from apps.core.models import RegistrationMode
from apps.dashboard.services.batch import UserBatchActions
from apps.plans.models import Plan
from apps.plans.services.binding import PlanBinding
from apps.plans.services.limits import assign_plan
from apps.plans.tests.factories import PlanFactory

pytestmark = pytest.mark.django_db


def moderator(**fields: object) -> User:
    return UserFactory(role=Role.MODERATOR, **fields)


def staffed(*members: User, **fields: object) -> Plan:
    plan: Plan = PlanFactory(**fields)
    plan.staff.add(*members)
    return plan


def links(user: User) -> set[tuple[str, bool]]:
    """The user's staff members by name, with whether the plan gave the link."""
    return {
        (link.member.username, link.via_plan)
        for link in Management.objects.filter(user=user).select_related("member")
    }


class TestAssign:
    def test_plan_links_its_staff(self) -> None:
        anna, boris = moderator(username="anna"), moderator(username="boris")
        user = UserFactory()

        assign_plan(user, staffed(anna, boris))

        assert links(user) == {("anna", True), ("boris", True)}

    def test_another_plan_replaces_the_links_and_keeps_manual_ones(self) -> None:
        anna, boris, vera = (moderator(username=name) for name in ("anna", "boris", "vera"))
        user = UserFactory(managers=[vera])
        assign_plan(user, staffed(anna, boris))

        assign_plan(user, staffed(boris, UserFactory(role=Role.OWNER, username="olga")))

        assert links(user) == {("boris", True), ("olga", True), ("vera", False)}

    def test_no_plan_drops_the_links_of_the_plan(self) -> None:
        vera = moderator(username="vera")
        user = UserFactory(managers=[vera])
        assign_plan(user, staffed(moderator(username="anna")))

        assign_plan(user, None)

        assert links(user) == {("vera", False)}

    def test_a_manual_link_stays_manual_without_a_duplicate(self) -> None:
        anna = moderator(username="anna")
        user = UserFactory(managers=[anna])
        plan = staffed(anna)

        assign_plan(user, plan)
        assign_plan(user, None)

        assert links(user) == {("anna", False)}

    def test_admins_on_the_list_are_not_linked(self) -> None:
        user = UserFactory()

        assign_plan(user, staffed(UserFactory(role=Role.ADMIN)))

        assert links(user) == set()


class TestOtherWays:
    def test_trial_plan_at_registration(self) -> None:
        trial = staffed(moderator(username="anna"), is_trial=True)
        site = configure(registration_mode=RegistrationMode.OPEN_AUTO, default_plan=trial)

        user = Registration(site, Letters("http://panel.example.com/")).register(
            Applicant("alice", "alice@example.com", "a long password 42")
        )

        assert links(user) == {("anna", True)}

    def test_batch_change_of_plan(self) -> None:
        user = UserFactory()
        assign_plan(user, staffed(moderator(username="anna")))

        UserBatchActions(UserFactory(role=Role.ADMIN)).change_plan(
            [user], staffed(moderator(username="boris"))
        )

        assert links(user) == {("boris", True)}


class TestQuota:
    def test_a_full_moderator_is_skipped_and_journaled(self) -> None:
        full = moderator(username="full", user_quota=1)
        UserFactory(managers=[full])
        user = UserFactory()

        assign_plan(user, staffed(full, moderator(username="anna")))

        assert links(user) == {("anna", True)}
        event = AuditEvent.objects.get(action="plans.binding_skipped")
        assert (event.target_id, event.data["member_id"]) == (str(user.pk), full.pk)


class TestStaffChanges:
    def test_plan_staff_change_relinks_its_users(self) -> None:
        anna, boris = moderator(username="anna"), moderator(username="boris")
        plan = staffed(anna)
        users = [UserFactory(), UserFactory()]
        for user in users:
            assign_plan(user, plan)

        plan.staff.add(boris)
        plan.staff.remove(anna)

        assert [links(user) for user in users] == [{("boris", True)}] * 2

    def test_clearing_either_side_drops_the_links(self) -> None:
        anna, boris = moderator(username="anna"), moderator(username="boris")
        plan = staffed(anna, boris)
        user = UserFactory()
        assign_plan(user, plan)

        anna.staffed_plans.clear()
        assert links(user) == {("boris", True)}

        plan.staff.clear()
        assert links(user) == set()

    def test_member_side_add_links_the_users(self) -> None:
        plan = staffed()
        user = UserFactory()
        assign_plan(user, plan)

        moderator(username="anna").staffed_plans.add(plan)

        assert links(user) == {("anna", True)}

    def test_full_moderator_added_to_staff_is_skipped_and_manual_links_stay(self) -> None:
        full = moderator(username="full", user_quota=1)
        vera = moderator(username="vera")
        plan = staffed()
        user = UserFactory(managers=[vera])
        assign_plan(user, plan)
        UserFactory(managers=[full])

        plan.staff.add(full)

        assert links(user) == {("vera", False)}
        assert AuditEvent.objects.filter(action="plans.binding_skipped").count() == 1

    def test_resync_is_repeatable(self) -> None:
        plan = staffed(moderator(username="anna"))
        user = UserFactory()
        assign_plan(user, plan)

        PlanBinding.resync(plan)
        PlanBinding.resync(plan)

        assert Management.objects.filter(user=user).count() == 1
