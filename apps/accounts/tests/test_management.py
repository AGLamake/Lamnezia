import pytest
from django.core.exceptions import ValidationError

from apps.accounts.models import Management, Role, User
from apps.accounts.services.management import Managers
from apps.accounts.services.zone import Zone
from apps.accounts.tests.factories import UserFactory
from apps.plans.tests.factories import PlanFactory
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def moderator(**fields: object) -> User:
    return UserFactory(role=Role.MODERATOR, **fields)


class TestManagers:
    def test_a_user_with_two_staff_members_is_in_the_zone_of_both(self) -> None:
        first, second, stranger = moderator(), moderator(), moderator()
        user = UserFactory()

        Managers(user).add(first)
        Managers(user).add(second)

        assert list(Zone(first).users()) == [user]
        assert list(Zone(second).users()) == [user]
        assert not Zone(stranger).users().exists()
        assert not Management.objects.get(member=first).via_plan

    def test_the_user_quota_stops_linking_by_hand(self) -> None:
        member = moderator(user_quota=1)
        Managers(UserFactory()).add(member)

        with pytest.raises(ValidationError, match="as many users as allowed"):
            Managers(UserFactory()).add(member)
        assert member.managed_users.count() == 1
        assert member.is_over_user_quota()

    def test_nobody_is_their_own_staff_member(self) -> None:
        member = moderator()

        with pytest.raises(ValidationError, match="their own moderator"):
            Managers(member).add(member)

    def test_a_link_is_made_once(self) -> None:
        member, user = moderator(), UserFactory()
        Managers(user).add(member)

        with pytest.raises(ValidationError):
            Managers(user).add(member)

    def test_removing_a_link_takes_the_user_out_of_the_zone(self) -> None:
        member, user = moderator(user_quota=1), UserFactory()
        Managers(user).add(member)

        Managers(user).remove(member)

        assert not Zone(member).users().exists()
        assert not member.is_over_user_quota()


class TestZone:
    def test_a_moderator_gets_the_plans_they_staff_and_an_admin_all(self) -> None:
        member = moderator()
        staffed, other = PlanFactory(), PlanFactory()
        staffed.staff.add(member)

        assert list(Zone(member).plans()) == [staffed]
        assert set(Zone(UserFactory(role=Role.ADMIN)).plans()) == {staffed, other}

    def test_a_moderator_offers_the_administrators_plans_and_their_own_not_an_owners(
        self,
    ) -> None:
        member, owner = moderator(), UserFactory(role=Role.OWNER)
        admins, staffed, owners = PlanFactory(), PlanFactory(), PlanFactory()
        staffed.staff.add(member, owner)
        owners.staff.add(owner)

        assert set(Zone(member).offered_plans()) == {admins, staffed}
        assert set(Zone(owner).offered_plans()) == {staffed, owners}

    def test_the_servers_stay_those_given_to_the_moderator(self) -> None:
        member = moderator()
        given, other = ServerFactory(), ServerFactory()
        member.moderated_servers.add(given)

        assert Zone(member).has_server(given.pk)
        assert not Zone(member).has_server(other.pk)
