"""Background jobs of the support app: the reminder of an answer the user has not seen."""

from celery import shared_task

from apps.support.models import Message
from apps.support.services.reminder import AnswerReminder


@shared_task(name="support.remind_of_answer")
def remind_of_answer(answer_id: int) -> None:
    answer = Message.objects.select_related("conversation__user").filter(pk=answer_id).first()
    if answer is not None:
        AnswerReminder(answer).send()
