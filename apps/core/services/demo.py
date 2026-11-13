"""Demo data for screenshots and trying the interface: made-up servers, users, keys and the rest.

Nothing goes over SSH: the servers have documentation addresses and the readings are invented.
Every record is found by a name of its own first, so seeding again adds nothing new.
"""

import random
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from amnezia_core.awg_install import generate_awg_params
from amnezia_core.keys import KeyPair, generate_preshared_key
from apps.accounts.models import Role, User
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import ServerProfile
from apps.notifications.events import Event
from apps.notifications.models import Channel, ChannelKind, Delivery, DeliveryStatus, Notification
from apps.plans.models import Plan, UserLimits
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.servers.models import AuthMethod, Server, ServerSample, ServerStatus
from apps.support.models import Conversation, Incident, Kind, Message, TicketStatus, TicketType
from apps.traffic.models import TrafficCounter, TrafficHourly

PASSWORD = "demo-password"
AWG2 = "amnezia-awg2"
GIB = 1024**3
_TRAFFIC_HOURS = 72


@dataclass(frozen=True)
class DemoServer:
    name: str
    host: str
    location: str
    port: int
    subnet: str


SERVERS = (
    DemoServer("Frankfurt", "203.0.113.10", "Germany", 55424, "10.8.1.0/24"),
    DemoServer("Amsterdam", "203.0.113.20", "Netherlands", 41738, "10.8.2.0/24"),
    DemoServer("Helsinki", "198.51.100.30", "Finland", 38261, "10.8.3.0/24"),
)
# Username, plan, devices.
USERS = (
    ("anna", "Basic", ("iPhone", "Laptop")),
    ("boris", "Premium", ("Android", "Desktop", "iPad")),
    ("vera", "Trial", ("Phone",)),
    ("gleb", "Basic", ("Router",)),
)
PLANS: tuple[tuple[str, dict[str, object]], ...] = (
    ("Trial", {"traffic_limit": 5 * GIB, "access_days": 7, "max_keys": 1, "is_trial": True}),
    ("Basic", {"traffic_limit": 100 * GIB, "max_keys": 3, "price": 300, "currency": "RUB"}),
    ("Premium", {"max_keys": 10, "speed_limit": 100_000, "price": 700, "currency": "RUB"}),
)


class DemoSeed:
    """Fills the database with demo records; `run` may be called any number of times."""

    def __init__(self, seed: int = 2026) -> None:
        self._rng = random.Random(seed)
        self._now = timezone.now()

    @transaction.atomic
    def run(self) -> None:
        self._user("admin", Role.ADMIN)
        self._user("moderator", Role.MODERATOR)
        instances = [self._instance(self._server(demo), demo) for demo in SERVERS]
        plans = {name: self._plan(name, fields) for name, fields in PLANS}
        for index, (username, plan_name, devices) in enumerate(USERS):
            user = self._user(username, Role.USER)
            UserLimits.objects.get_or_create(
                user=user, defaults={"plan": plans[plan_name], "plan_assigned_at": self._now}
            )
            for number, device in enumerate(devices):
                instance = instances[(index + number) % len(instances)]
                self._traffic(self._key(user, instance, device, number + 2 + index * 10))
        self._support(instances[-1].server)
        self._notifications()

    def _user(self, username: str, role: Role) -> User:
        user, created = User.objects.get_or_create(
            username=username, defaults={"role": role, "email": f"{username}@example.com"}
        )
        if created:
            user.set_password(PASSWORD)
            user.save(update_fields=["password"])
        return user

    def _server(self, demo: DemoServer) -> Server:
        server, created = Server.objects.get_or_create(
            name=demo.name,
            defaults={
                "host": demo.host,
                "location": demo.location,
                "auth_method": AuthMethod.PASSWORD,
                "password": PASSWORD,
                "status": ServerStatus.READY,
                "os_name": "Ubuntu 24.04 LTS",
                "docker_version": "27.3.1",
                "disk_free_bytes": 18 * GIB,
                "containers": [{"name": AWG2, "port": str(demo.port), "transport": "udp"}],
                "checked_at": self._now,
                "last_seen_at": self._now,
            },
        )
        if created:
            self._samples(server)
        return server

    def _samples(self, server: Server) -> None:
        sample = None
        for minutes in range(120, -1, -5):
            sample = ServerSample.objects.create(
                server=server,
                at=self._now - timedelta(minutes=minutes),
                cpu_percent=round(self._rng.uniform(3, 45), 1),
                memory_total=2 * GIB,
                memory_available=int(self._rng.uniform(0.6, 1.4) * GIB),
                disk_total=40 * GIB,
                disk_free=18 * GIB,
                uptime=86_400 * 12 + (120 - minutes) * 60,
                containers=[{"name": AWG2, "state": "running"}],
            )
        Server.objects.filter(pk=server.pk).update(latest_sample=sample)

    def _instance(self, server: Server, demo: DemoServer) -> ProtocolInstance:
        pair = KeyPair.generate()
        instance, _ = ProtocolInstance.objects.get_or_create(
            server=server,
            container=AWG2,
            defaults={
                "port": demo.port,
                "subnet": demo.subnet,
                "params": generate_awg_params(self._rng),
                "public_key": pair.public_key,
                "private_key": pair.private_key,
                "preshared_key": generate_preshared_key(),
                "origin": InstanceOrigin.INSTALLED,
            },
        )
        return instance

    def _plan(self, name: str, fields: dict[str, object]) -> Plan:
        plan, _ = Plan.objects.get_or_create(name=name, defaults=fields)
        return plan

    def _key(self, user: User, instance: ProtocolInstance, device: str, host: int) -> AccessKey:
        existing = AccessKey.objects.filter(user=user, source=instance.source, name=device).first()
        if existing is not None:
            return existing
        pair = KeyPair.generate()
        profile = ServerProfile(
            AWG2,
            instance.server.host,
            instance.port,
            instance.subnet,
            dict(instance.params),
            instance.public_key,
            instance.preshared_key,
        )
        network = instance.subnet.rsplit(".", 1)[0]
        return AccessKey.objects.create(
            user=user,
            source=instance.source,
            name=device,
            public_key=pair.public_key,
            private_key=pair.private_key,
            profile=profile.to_json(),
            client_ip=f"{network}.{host}",
            status=KeyStatus.ACTIVE,
        )

    def _traffic(self, key: AccessKey) -> None:
        if TrafficCounter.objects.filter(source=key.source, public_key=key.public_key).exists():
            return
        hour = self._now.replace(minute=0, second=0, microsecond=0)
        TrafficHourly.objects.bulk_create(
            TrafficHourly(
                source=key.source,
                public_key=key.public_key,
                hour=hour - timedelta(hours=back),
                rx_bytes=self._rng.randint(0, 400) * 1024**2,
                tx_bytes=self._rng.randint(0, 60) * 1024**2,
            )
            for back in range(_TRAFFIC_HOURS)
        )
        TrafficCounter.objects.create(
            source=key.source,
            public_key=key.public_key,
            rx_bytes=self._rng.randint(1, 50) * GIB,
            tx_bytes=self._rng.randint(1, 5) * GIB,
            latest_handshake=self._now - timedelta(seconds=self._rng.randint(10, 600)),
            collected_at=self._now,
        )

    def _support(self, troubled: Server) -> None:
        anna, boris = User.objects.get(username="anna"), User.objects.get(username="boris")
        staff = User.objects.get(username="moderator")
        self._conversation(
            anna,
            Kind.TICKET,
            "The key does not import",
            [(anna, "The app says the config is wrong."), (staff, "Update the app, please.")],
            type=TicketType.BUG,
            status=TicketStatus.WAITING_USER,
        )
        self._conversation(
            boris,
            Kind.TICKET,
            "Helsinki is slow",
            [(boris, "Speed dropped to 1 Mbit/s since the morning.")],
            type=TicketType.SERVER_DOWN,
            status=TicketStatus.NEW,
            server=troubled,
        )
        self._conversation(boris, Kind.CHAT, "", [(boris, "Hello! Can I pay for a year?")])
        if not Incident.objects.filter(server=troubled).exists():
            Incident.objects.create(server=troubled)

    def _conversation(
        self,
        user: User,
        kind: Kind,
        subject: str,
        messages: list[tuple[User, str]],
        **fields: object,
    ) -> None:
        conversation, created = Conversation.objects.get_or_create(
            user=user, kind=kind, subject=subject, defaults=fields
        )
        if created:
            Message.objects.bulk_create(
                Message(conversation=conversation, author=author, text=text)
                for author, text in messages
            )

    def _notifications(self) -> None:
        channel, _ = Channel.objects.get_or_create(
            name="Demo webhook",
            defaults={"kind": ChannelKind.WEBHOOK, "target": "https://example.com/hook"},
        )
        events = (
            (Event.SERVER_DOWN, {"server": "Helsinki", "host": "198.51.100.30", "last_seen": ""}),
            (Event.CHAT_MESSAGE, {"user": "boris", "text": "Hello! Can I pay for a year?"}),
            (Event.TICKET_CREATED, {"user": "anna", "type": "Something is broken"}),
        )
        for number, (event, data) in enumerate(events):
            notification, created = Notification.objects.get_or_create(
                key=f"demo:{number}", defaults={"event": event, "data": data}
            )
            if created:
                Delivery.objects.create(
                    notification=notification,
                    channel=channel,
                    channel_name=str(channel),
                    status=DeliveryStatus.SENT if number else DeliveryStatus.FAILED,
                    attempts=1,
                    error="" if number else "HTTP 404",
                    sent_at=self._now if number else None,
                )
