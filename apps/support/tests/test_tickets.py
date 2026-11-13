from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.notifications.events import Event
from apps.notifications.models import Channel, ChannelKind, Notification, Route
from apps.operations.models import Operation
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.servers.tests.test_monitor import FakeServers
from apps.support.models import Conversation, TicketStatus, TicketType
from apps.support.services.check import CHECK_KIND, CheckReport, RequestServerCheck
from apps.support.services.images import MAX_IMAGE_SIZE, image_type, validate_image
from apps.support.services.thread import TooManyMessagesError, post
from apps.support.services.tickets import (
    ClosedConversationError,
    MyRequests,
    ServerNotAllowedError,
    TooManyRequestsError,
)
from apps.traffic.models import TrafficCounter

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 20
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"


def png(name: str = "shot.png", content: bytes = PNG) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="image/png")


def subscriber(*servers: Server, plan_name: str | None = None) -> User:
    user = UserFactory()
    plan = PlanFactory(**({"name": plan_name} if plan_name else {}))
    plan.servers.set(servers)
    UserLimitsFactory(user=user, plan=plan, plan_assigned_at=timezone.now())
    return user


def instance_on(server: Server) -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=server,
        container="amnezia-awg2",
        port=443,
        subnet="10.8.1.0/24",
        public_key="pub",
        private_key="priv",
        origin=InstanceOrigin.IMPORTED,
    )


class TestImages:
    @pytest.mark.parametrize(
        ("head", "kind"),
        [
            (PNG, "image/png"),
            (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
            (b"GIF89a", "image/gif"),
            (b"RIFF\0\0\0\0WEBPVP8 ", "image/webp"),
            (b"<svg xmlns=", None),
            (b"%PDF-1.7", None),
        ],
    )
    def test_images_are_told_by_their_first_bytes(self, head: bytes, kind: str | None) -> None:
        assert image_type(head) == kind

    def test_a_file_named_png_that_is_not_an_image_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_image(png("shot.png", b"<script>alert(1)</script>"))

    def test_an_image_over_5_mb_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            validate_image(png(content=PNG + b"\0" * MAX_IMAGE_SIZE))


class TestOpen:
    def test_the_request_keeps_the_text_screenshot_and_context(self) -> None:
        user = subscriber(plan_name="Friends")
        key = AccessKey.objects.create(
            user=user, source="instance:1", name="iPhone", public_key="k", status=KeyStatus.ACTIVE
        )
        seen = timezone.now() - timedelta(minutes=5)
        TrafficCounter.objects.create(
            source=key.source, public_key="k", latest_handshake=seen, collected_at=seen
        )

        conversation = MyRequests(user, IPHONE).open(
            TicketType.BUG, "No sites open\nSince the morning", png()
        )

        assert conversation.subject == "No sites open"
        assert conversation.status == TicketStatus.NEW
        (message,) = conversation.messages.all()
        assert message.author == user
        assert message.attachments.get().content_type == "image/png"
        context = conversation.context
        assert context["plan"] == "Friends"
        assert context["device"] == "ios"
        assert context["user_agent"] == IPHONE
        assert context["keys"] == [
            {
                "name": "iPhone",
                "server": "instance:1",
                "status": "active",
                "handshake": seen.isoformat(),
            }
        ]

    def test_an_xray_key_is_seen_by_its_traffic(self) -> None:
        user = subscriber()
        key = AccessKey.objects.create(
            user=user, source="instance:1", name="Mac", public_key="x", status=KeyStatus.ACTIVE
        )
        seen = timezone.now() - timedelta(minutes=2)
        TrafficCounter.objects.create(
            source=key.source, public_key="x", active_at=seen, collected_at=seen
        )

        conversation = MyRequests(user, IPHONE).open(TicketType.BUG, "Slow")

        assert conversation.context["keys"][0]["handshake"] == seen.isoformat()

    def test_staff_hear_of_a_new_request(self) -> None:
        channel = Channel.objects.create(
            name="admins", kind=ChannelKind.WEBHOOK, target="https://x"
        )
        Route.objects.create(channel=channel, event=Event.TICKET_CREATED)
        user = subscriber()

        conversation = MyRequests(user).open(TicketType.QUESTION, "How do I share a key?")

        notification = Notification.objects.get(event=Event.TICKET_CREATED)
        assert notification.data == {
            "user": user.username,
            "number": conversation.pk,
            "type": "Question",
            "subject": "How do I share a key?",
        }

    @override_settings(SUPPORT_RATE_LIMIT=2)
    def test_too_many_requests_an_hour_are_refused(self) -> None:
        requests = MyRequests(subscriber())
        requests.open(TicketType.BUG, "one")
        requests.open(TicketType.BUG, "two")

        with pytest.raises(TooManyRequestsError):
            requests.open(TicketType.BUG, "three")

        Conversation.objects.update(created_at=timezone.now() - timedelta(hours=2))
        requests.open(TicketType.BUG, "later")


class TestServerDown:
    def test_one_click_opens_a_request_and_starts_a_check(self) -> None:
        server = ServerFactory(name="Amsterdam")
        user = subscriber(server)

        conversation = MyRequests(user).report_server(server)

        assert conversation.type == TicketType.SERVER_DOWN
        assert conversation.server == server
        assert conversation.messages.count() == 0
        operation = conversation.check_operation
        assert isinstance(operation, Operation)
        assert operation.kind == CHECK_KIND
        assert operation.params == {"conversation_id": conversation.pk}

    def test_a_second_report_adds_to_the_open_request(self) -> None:
        server = ServerFactory()
        requests = MyRequests(subscriber(server))
        first = requests.report_server(server)

        again = requests.report_server(server, "Still down")

        assert again == first
        assert [m.text for m in first.messages.all()] == ["Still down"]
        assert Operation.objects.count() == 1

    def test_a_server_outside_the_plan_cannot_be_reported(self) -> None:
        allowed, other = ServerFactory(), ServerFactory()

        with pytest.raises(ServerNotAllowedError):
            MyRequests(subscriber(allowed)).report_server(other)

    def test_the_check_result_is_attached_to_the_request(self) -> None:
        server = ServerFactory()
        instance = instance_on(server)
        user = subscriber(server)
        conversation = MyRequests(user).report_server(server)
        mine = AccessKey.objects.create(
            user=user, source=instance.source, name="phone", public_key="mine"
        )
        now = timezone.now()
        for public_key, ago in ((mine.public_key, 30), ("someone", 1)):
            TrafficCounter.objects.create(
                source=instance.source,
                public_key=public_key,
                latest_handshake=now - timedelta(minutes=ago),
                collected_at=now,
            )

        RequestServerCheck(FakeServers().connect).run(conversation, server)

        conversation.refresh_from_db()
        report = CheckReport.from_json(conversation.server_check)
        assert report is not None
        assert report.answered
        assert report.user_seen == now - timedelta(minutes=30)
        assert report.server_seen == now - timedelta(minutes=1)

    def test_xray_clients_are_seen_by_their_traffic(self) -> None:
        server = ServerFactory()
        instance = instance_on(server)
        user = subscriber(server)
        conversation = MyRequests(user).report_server(server)
        mine = AccessKey.objects.create(
            user=user, source=instance.source, name="phone", public_key="mine"
        )
        now = timezone.now()
        TrafficCounter.objects.create(
            source=instance.source,
            public_key=mine.public_key,
            latest_handshake=now - timedelta(hours=2),
            active_at=now - timedelta(minutes=3),
            collected_at=now,
        )

        report = RequestServerCheck(FakeServers().connect).run(conversation, server)

        assert report.user_seen == now - timedelta(minutes=3)
        assert report.server_seen == now - timedelta(minutes=3)

    def test_a_silent_server_is_reported_as_not_answering(self) -> None:
        server = ServerFactory(name="down")
        conversation = MyRequests(subscriber(server)).report_server(server)
        servers = FakeServers()
        servers.down.add("down")

        report = RequestServerCheck(servers.connect).run(conversation, server)

        assert not report.answered
        assert not report.ok
        assert report.user_seen is None


class TestThread:
    def test_the_user_never_sees_internal_notes(self) -> None:
        user, admin = subscriber(), UserFactory(role="admin")
        requests = MyRequests(user)
        conversation = requests.open(TicketType.QUESTION, "Hello")
        post(conversation, admin, "Looks like a client bug", internal=True)
        post(conversation, admin, "Update the app, please")

        assert [m.text for m in requests.messages(conversation)] == [
            "Hello",
            "Update the app, please",
        ]

    def test_a_reply_to_a_waiting_request_puts_it_back_to_work(self) -> None:
        requests = MyRequests(subscriber())
        conversation = requests.open(TicketType.QUESTION, "Hello")
        Conversation.objects.filter(pk=conversation.pk).update(status=TicketStatus.WAITING_USER)
        conversation.refresh_from_db()

        requests.reply(conversation, "Here is the log")

        conversation.refresh_from_db()
        assert conversation.status == TicketStatus.IN_PROGRESS

    @override_settings(SUPPORT_MESSAGE_RATE_LIMIT=2)
    def test_replies_to_a_request_are_limited_a_minute(self) -> None:
        requests = MyRequests(subscriber())
        conversation = requests.open(TicketType.QUESTION, "Hello")  # the first message
        Conversation.objects.filter(pk=conversation.pk).update(status=TicketStatus.WAITING_USER)
        conversation.refresh_from_db()
        requests.reply(conversation, "Here is the log")

        with pytest.raises(TooManyMessagesError):
            requests.reply(conversation, "And one more")
        assert conversation.messages.count() == 2

    def test_a_closed_request_takes_no_replies(self) -> None:
        requests = MyRequests(subscriber())
        conversation = requests.open(TicketType.QUESTION, "Hello")
        conversation.status = TicketStatus.CLOSED

        with pytest.raises(ClosedConversationError):
            requests.reply(conversation, "One more thing")

    def test_reading_the_thread_marks_staff_messages_read(self) -> None:
        user, admin = subscriber(), UserFactory(role="admin")
        requests = MyRequests(user)
        conversation = requests.open(TicketType.QUESTION, "Hello")
        answer = post(conversation, admin, "Hi")

        requests.mark_read(conversation)

        answer.refresh_from_db()
        assert answer.read_at is not None
        assert conversation.messages.get(author=user).read_at is None
