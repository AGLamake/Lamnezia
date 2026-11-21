"""A planned switch of a relayed group to a chosen member, for maintenance of the active server.

The panel puts a switch request next to the agent config and waits for the answer in the agent
state. The agent moves the group the way it does on a failure (one `nft -f`, then the
conntrack entries of the port go), and only if the member answers a probe right then. The
clients of the group reconnect: an outage of about 20 seconds. An agent of an older release
does not answer; the request is then taken back. A member that leaves its group while the
relay sends the clients to it is switched away from first (`LeaveSwitch`).
"""

import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext as _

from amnezia_core.errors import AmneziaFormatError
from amnezia_core.relay import RelayState, SwitchOutcome, SwitchRequest, SwitchResult
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.replication import backends
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import Relay
from apps.relays.services.config import member_backend
from apps.relays.services.container import FOLDER, NAME
from apps.relays.services.monitor import STATE_PATH, RelayMonitor
from apps.servers.services.connection import connect
from apps.ssh.errors import RemoteFileNotFoundError, SshError
from apps.ssh.services import DockerContainer

SWITCH_KIND = "relays.switch"
REQUEST_PATH = f"{FOLDER}/switch.json"
# The agent takes a request on its next pass, every few seconds, and probes for about a second.
ANSWER_TIMEOUT = 60.0
ANSWER_POLL = 2.0

_REFUSALS = {
    SwitchOutcome.UNREACHABLE: RelayErrorCode.SWITCH_UNREACHABLE,
    SwitchOutcome.UNKNOWN_GROUP: RelayErrorCode.SWITCH_OUTDATED,
    SwitchOutcome.UNKNOWN_BACKEND: RelayErrorCode.SWITCH_OUTDATED,
}
_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


def start_switch(member: GroupMember, *, user: User, ip: str | None = None) -> Operation:
    """Start moving the clients of the member's group to it through the relay."""
    with transaction.atomic():
        member = GroupMember.objects.select_related("group", "instance").get(pk=member.pk)
        group = member.group
        installed = Relay.objects.filter(installed_at__isnull=False)
        relay = installed.filter(server_id=group.relay_id).first() if group.relay_id else None
        if relay is None:
            raise RelayError(RelayErrorCode.NOT_INSTALLED, group.name)
        if member not in backends(group):
            raise RelayError(RelayErrorCode.SWITCH_NOT_BACKEND, str(member.instance))
        running = Operation.objects.filter(
            kind=SWITCH_KIND, status__in=_UNFINISHED, params__group_id=group.pk
        )
        if running.exists():
            raise RelayError(RelayErrorCode.SWITCH_RUNNING, group.name)
        params = {"server_id": relay.server_id, "group_id": group.pk, "member_id": member.pk}
        operation = launch(SWITCH_KIND, params, user=user, subject=str(member))
        data = {"operation": operation.pk, "instance": member.instance_id}
        audit.log(SWITCH_KIND, actor=user, target=group, data=data, ip=ip)
    return operation


def last_switch(group: ServerGroup) -> Operation | None:
    """The newest planned switch of the group, finished or still under way."""
    switches = Operation.objects.filter(kind=SWITCH_KIND, params__group_id=group.pk)
    return switches.order_by("-created_at", "-pk").first()


class RelaySwitch:
    """One switch operation; `pause` and `clock` wait for the agent and are replaced in tests."""

    def __init__(
        self,
        relay: Relay,
        member: GroupMember,
        ctx: OperationContext,
        *,
        pause: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._relay = relay
        self._member = member
        self._ctx = ctx
        self._pause = pause
        self._clock = clock

    def run(self) -> None:
        request = self._request()
        with ExitStack() as stack:
            with self._ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(self._relay.server))
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            agent = host.container(NAME)
            with self._ctx.step(_("Ask the relay to switch")):
                agent.write_file_atomic(REQUEST_PATH, request.to_json().encode(), mode=0o600)
                self._ctx.log(f"{request.port}/udp → {request.backend}")
            with self._ctx.step(_("Wait for the answer of the relay")):
                outcome = self._answer(agent, request.id)
                if outcome in _REFUSALS:
                    raise RelayError(_REFUSALS[outcome], str(request.backend))
                if outcome == SwitchOutcome.ALREADY_ACTIVE:
                    self._ctx.log(_("The relay already sends the clients to this server."))
                    return
                self._ctx.log(_("The relay sends the clients of the group to this server now."))
        with self._ctx.step(_("Read the relay state")):
            interval = timedelta(seconds=settings.RELAY_MONITOR_INTERVAL)
            RelayMonitor(interval, connect=connect).poll(self._relay)

    def _request(self) -> SwitchRequest:
        with self._ctx.step(_("Find the address of the server")):
            try:
                backend = member_backend(self._member)
            except OSError as error:
                raise RelayError(RelayErrorCode.HOST_NOT_RESOLVED, str(error)) from error
        return SwitchRequest(uuid.uuid4().hex, self._member.group.port, backend)

    def _answer(self, agent: DockerContainer, request: str) -> SwitchOutcome:
        """The agent's answer to this request; a request left unanswered is taken back."""
        deadline = self._clock() + ANSWER_TIMEOUT
        while self._clock() < deadline:
            self._pause(ANSWER_POLL)
            answer = _read_answer(agent)
            if answer is not None and answer.id == request:
                return answer.outcome
        agent.run(f"rm -f -- {REQUEST_PATH}")
        raise RelayError(RelayErrorCode.SWITCH_NO_ANSWER, NAME)


class LeaveSwitch:
    """Before a member leaves its relayed group, the planned switch of its clients to the next
    member, so they reconnect as after a switch and not after a failure; a member the relay
    does not send the clients to needs none.

    When no other member is left or the switch fails, the leave goes on and its log warns that
    the clients reconnect only once the relay finds the server gone.
    """

    def __init__(
        self,
        member: GroupMember,
        ctx: OperationContext,
        *,
        pause: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._member = member
        self._group = member.group
        self._ctx = ctx
        self._pause = pause
        self._clock = clock

    def run(self) -> None:
        relay = self._relay()
        if relay is None:
            return
        with self._ctx.step(_("Find a server for the clients of the relay")):
            target = self._target(relay)
            if target is None:
                return
            self._ctx.log(str(target.instance))
        try:
            RelaySwitch(relay, target, self._ctx, pause=self._pause, clock=self._clock).run()
        except (RelayError, SshError) as error:
            with self._ctx.step(_("Leave without a planned switch")):
                self._ctx.log(error.message)
                self._warn()

    def _relay(self) -> Relay | None:
        if self._group.relay_id is None:
            return None
        installed = Relay.objects.filter(installed_at__isnull=False).select_related("server")
        return installed.filter(server_id=self._group.relay_id).first()

    def _target(self, relay: Relay) -> GroupMember | None:
        """The first other member the relay may send the clients to; None when the relay
        sends them elsewhere already or no such member is left."""
        health = relay.group_health(self._group.port)
        active = health.active if health is not None else None
        if active is not None and active.id != self._member.pk:
            self._ctx.log(_("The relay sends the clients to another server: no switch needed."))
            return None
        for member in backends(self._group):
            if member.pk != self._member.pk and (
                health is None or health.reachable(member.pk) is not False
            ):
                return member
        self._ctx.log(_("No other server of the group answers the relay."))
        self._warn()
        return None

    def _warn(self) -> None:
        self._ctx.log(
            _(
                "Warning: the clients of the relay lose the connection until the relay finds "
                "this server gone."
            )
        )


def _read_answer(agent: DockerContainer) -> SwitchResult | None:
    try:
        return RelayState.parse(agent.read_file(STATE_PATH).decode("utf-8")).switch
    except (RemoteFileNotFoundError, AmneziaFormatError, UnicodeDecodeError):
        return None
