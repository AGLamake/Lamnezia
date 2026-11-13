import pytest

from apps.accounts.models import Messenger
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.support.models import TicketStatus, TicketType
from apps.support.services.chat import MyChat
from apps.support.services.inbox import Inbox
from apps.support.services.tickets import MyRequests
from apps.support.tests.test_inbox import admin, request_of
from apps.support.tests.test_tickets import subscriber

pytestmark = pytest.mark.django_db


class TestAnswerToAMessenger:
    def test_a_chat_answer_goes_to_the_messenger_the_user_wrote_from(self) -> None:
        user = subscriber()
        user.email = ""
        user.save()
        MyChat(user, Messenger.TELEGRAM).send("The key does not import")
        conversation = user.conversations.get()

        Inbox(admin()).reply(conversation, "Import it again")

        notification = Notification.objects.get(event=Event.CHAT_ANSWER)
        assert notification.via == Messenger.TELEGRAM
        assert notification.user == user
        assert notification.data["text"] == "Import it again"

    def test_a_request_answer_names_the_topic(self) -> None:
        user = subscriber()
        conversation = MyRequests(user, via=Messenger.DISCORD).open(TicketType.BUG, "No handshake")

        Inbox(admin()).reply(conversation, "Fixed")

        notification = Notification.objects.get(event=Event.REQUEST_ANSWER)
        assert (notification.via, notification.data["topic"]) == ("discord", "No handshake")

    def test_the_latest_message_decides_where_the_answer_goes(self) -> None:
        user = subscriber()
        conversation = MyRequests(user, via=Messenger.TELEGRAM).open(TicketType.BUG, "Broken")
        MyRequests(user).reply(conversation, "Writing from the site now")

        Inbox(admin()).reply(conversation, "Thanks")

        assert not Notification.objects.filter(event=Event.REQUEST_ANSWER).exists()


class TestStatus:
    def test_the_user_hears_that_a_request_is_resolved(self) -> None:
        user = subscriber()
        conversation = request_of(user)

        Inbox(admin()).set_status(conversation, TicketStatus.RESOLVED)

        notification = Notification.objects.get(event=Event.REQUEST_STATUS)
        assert notification.user == user
        assert notification.data["status"] == "resolved"

    def test_taking_a_request_into_work_or_keeping_the_status_says_nothing(self) -> None:
        conversation = request_of(subscriber())
        staff = Inbox(admin())

        staff.set_status(conversation, TicketStatus.IN_PROGRESS)
        staff.set_status(conversation, TicketStatus.IN_PROGRESS)

        assert not Notification.objects.filter(event=Event.REQUEST_STATUS).exists()
