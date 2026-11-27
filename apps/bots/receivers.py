"""Model signals that keep the Telegram menus in step, connected in `BotsConfig`.

A staff role given or taken away and a staff member's Telegram account linked or unlinked set
the menu of that chat again once the change is committed. Other users keep the common menu, so
their changes send nothing.
"""

from functools import partial
from typing import Any

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save

from apps.accounts.models import STAFF_ROLES, LinkedAccount, Messenger, User
from apps.bots.tasks import refresh_chat_menu


def on_account_changed(sender: object, instance: LinkedAccount, **kwargs: Any) -> None:
    if instance.messenger == Messenger.TELEGRAM and instance.user.role in STAFF_ROLES:
        _refresh(instance.external_id)


def on_user_saving(
    sender: object, instance: User, update_fields: Any = None, **kwargs: Any
) -> None:
    """Remember whether the save makes or unmakes a staff member; other fields are not read."""
    instance._staff_changes = False  # type: ignore[attr-defined]
    if instance.pk is None or (update_fields is not None and "role" not in update_fields):
        return
    was = User.objects.filter(pk=instance.pk).values_list("role", flat=True).first()
    instance._staff_changes = (was in STAFF_ROLES) != (  # type: ignore[attr-defined]
        instance.role in STAFF_ROLES
    )


def on_user_saved(sender: object, instance: User, **kwargs: Any) -> None:
    if not getattr(instance, "_staff_changes", False):
        return
    chats = LinkedAccount.objects.filter(user=instance, messenger=Messenger.TELEGRAM)
    for chat_id in chats.values_list("external_id", flat=True):
        _refresh(chat_id)


def _refresh(chat_id: str) -> None:
    transaction.on_commit(partial(refresh_chat_menu.delay, chat_id))


def connect() -> None:
    post_save.connect(on_account_changed, LinkedAccount, dispatch_uid="bots.account_saved")
    post_delete.connect(on_account_changed, LinkedAccount, dispatch_uid="bots.account_deleted")
    pre_save.connect(on_user_saving, User, dispatch_uid="bots.user_saving")
    post_save.connect(on_user_saved, User, dispatch_uid="bots.user_saved")
