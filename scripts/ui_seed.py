"""Demo data for the interface check: servers with load, a plan, users with keys, traffic and chat.

Run against an empty database: `python manage.py shell < scripts/ui_seed.py`.
Every user's password is `password`; `boss` is the administrator, `client` a regular user.
"""

import random
from datetime import timedelta

from django.utils import timezone

from amnezia_core.keys import KeyPair
from apps.accounts.models import Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey
from apps.operations.models import OperationStatus
from apps.operations.tests.factories import FailedOperationFactory, OperationFactory
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.protocols.models import ProtocolInstance
from apps.servers.models import ServerSample, ServerStatus
from apps.servers.tests.factories import ServerFactory
from apps.support.models import TicketType
from apps.support.services.chat import MyChat
from apps.support.services.inbox import Inbox
from apps.support.services.tickets import MyRequests
from apps.traffic.models import TrafficHourly

GB = 1024**3
random.seed(7)
now = timezone.now()

UserFactory(username="boss", role=Role.ADMIN)
UserFactory(username="newbie", email="newbie@example.com", status=Status.PENDING)

instances = []
for name, status, cpu in [
    ("Amsterdam", ServerStatus.READY, 23.0),
    ("Helsinki", ServerStatus.FAILED, None),
]:
    server = ServerFactory(name=name, status=status, paid_until=(now + timedelta(days=5)).date())
    for hour in range(24, -1, -1):
        server.latest_sample = ServerSample.objects.create(
            server=server,
            at=now - timedelta(hours=hour),
            cpu_percent=None if cpu is None else cpu + random.uniform(-15, 15),
            memory_total=4 * GB,
            memory_available=int(4 * GB * random.uniform(0.3, 0.7)),
            disk_total=80 * GB,
            disk_free=int(80 * GB * random.uniform(0.35, 0.5)),
            uptime=86400 * 12,
            error="" if cpu is not None else "host_unreachable",
        )
    server.save()
    instances.append(
        ProtocolInstance.objects.create(
            server=server,
            container="amnezia-awg2",
            port=55424,
            subnet="10.8.1.0/24",
            public_key=KeyPair.generate().public_key,
            private_key=KeyPair.generate().private_key,
            origin="imported",
        )
    )

plan = PlanFactory(name="Basic", access_days=30, traffic_limit=100 * GB, period_days=30, max_keys=3)
traffic = []
for number, username in enumerate(["client", "anna", "boris"]):
    user = UserFactory(username=username, email=f"{username}@example.com")
    UserLimitsFactory(user=user, plan=plan, plan_assigned_at=now - timedelta(days=10))
    for device in ["iPhone", "MacBook"][: 1 + number % 2]:
        pair = KeyPair.generate()
        key = AccessKey.objects.create(
            user=user,
            source=instances[0].source,
            name=device,
            public_key=pair.public_key,
            private_key=pair.private_key,
            client_ip=f"10.8.1.{AccessKey.objects.count() + 2}",
        )
        hour0 = now.replace(minute=0, second=0, microsecond=0)
        traffic += [
            TrafficHourly(
                source=key.source,
                public_key=key.public_key,
                hour=hour0 - timedelta(hours=hour),
                rx_bytes=random.randint(5, 120) * 1024**2,
                tx_bytes=random.randint(1, 20) * 1024**2,
            )
            for hour in range(0, 7 * 24, 3)
        ]
TrafficHourly.objects.bulk_create(traffic)

FailedOperationFactory()
OperationFactory(status=OperationStatus.SUCCEEDED)

# The client's chat in the widget: an answer and a line about an answered request.
client = User.objects.get(username="client")
staff = Inbox(User.objects.get(username="boss"))
chat = MyChat(client).send("Hello! Can I pay for a year?").conversation
staff.reply(chat, "Yes: choose the yearly plan on the Payment tab.")
staff.reply(MyRequests(client).open(TicketType.QUESTION, "The key does not import"), "Done.")
