"""Background tasks of the bots: the Telegram menu of a chat after a role or link change."""

from celery import shared_task

from apps.bots.services.chat_menu import ChatMenu
from apps.notifications.services.channels import DeliveryError


@shared_task(
    name="bots.refresh_chat_menu",
    autoretry_for=(DeliveryError,),
    retry_backoff=True,
    max_retries=5,
)
def refresh_chat_menu(chat_id: str) -> None:
    ChatMenu.from_settings().refresh(chat_id)
