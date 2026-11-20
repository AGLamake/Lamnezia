"""The staff members serving a user, linked and unlinked by hand within each one's quota."""

from django.db import transaction

from apps.accounts.models import Management, User


class Managers:
    """The staff members of one user. A link by hand is checked like in the admin site: nobody
    serves themselves, and a moderator takes no user past their quota.
    """

    def __init__(self, user: User) -> None:
        self._user = user

    def add(self, member: User) -> Management:
        """Link `member`; `ValidationError` when the link is not allowed or already there.

        The member's row stays locked until commit, so two links at once cannot both pass the
        quota.
        """
        with transaction.atomic():
            member = User.objects.select_for_update().get(pk=member.pk)
            link = Management(user=self._user, member=member)
            link.full_clean()
            link.save()
            return link

    def remove(self, member: User) -> None:
        Management.objects.filter(user=self._user, member=member).delete()
