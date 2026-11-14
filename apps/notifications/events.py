"""The events the panel notifies about and how each reads in a message.

Event details (`data`) are flat JSON with the keys the texts use; a webhook gets them as they are,
so a key, once released, is never renamed (docs/wiki: Notifications).
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from django.db import models
from django.utils import translation
from django.utils.functional import Promise
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.traffic.signals import LimitReason


class Event(StrEnum):
    SERVER_DOWN = "server_down"
    LIMIT_REACHED = "limit_reached"
    ACCESS_EXPIRING = "access_expiring"
    KEYS_CHANGED = "keys_changed"
    KEYS_REVOKED = "keys_revoked"
    KEYS_MOVED = "keys_moved"
    KEYS_UPDATED = "keys_updated"
    REGISTRATION_PENDING = "registration_pending"
    TICKET_CREATED = "ticket_created"
    CHAT_MESSAGE = "chat_message"
    SUPPORT_ANSWER = "support_answer"
    REQUEST_REMINDER = "request_reminder"
    CHAT_ANSWER = "chat_answer"
    REQUEST_ANSWER = "request_answer"
    REQUEST_STATUS = "request_status"
    INCIDENT_OPENED = "incident_opened"
    INCIDENT_CLOSED = "incident_closed"
    INCIDENT_RESOLVED = "incident_resolved"
    SUGGESTION_CREATED = "suggestion_created"
    SUGGESTION_ACCEPTED = "suggestion_accepted"
    SUGGESTION_REJECTED = "suggestion_rejected"
    PAYMENT_RECEIVED = "payment_received"
    RELAY_SWITCHED = "relay_switched"
    MEMBER_UNREACHABLE = "member_unreachable"
    GROUP_UNREACHABLE = "group_unreachable"
    RELAY_DOWN = "relay_down"
    MEMBER_LAGGING = "member_lagging"
    TEST = "test"


@dataclass(frozen=True)
class EventType:
    """`to_user`: the user the event is about hears of it too, besides the routed channels.

    `translated`: details that hold panel words in English (`english`), shown in the reader's
    language. `coded`: details that hold a choice value, which a webhook gets as it is and a
    message shows by its label. `joined`: occurrences about one user close in time make one
    notification. `fallback`: when the user's channel does not take it, their other channels
    are tried in turn.
    """

    label: Promise
    title: Promise
    text: Promise
    to_user: bool = False
    routable: bool = True
    translated: tuple[str, ...] = ()
    coded: Mapping[str, type[models.TextChoices]] = field(default_factory=dict)
    joined: bool = False
    fallback: bool = False

    def render(self, data: dict[str, Any]) -> tuple[str, str]:
        """Title and text in the active language with the details filled in; a missing detail
        reads as a dash.
        """
        values = defaultdict(lambda: "—", {key: self._value(key, v) for key, v in data.items()})
        text = str(self.text).format_map(values)
        if data.get("url"):
            text = f"{text}\n{data['url']}"
        return str(self.title).format_map(values), text

    def _value(self, key: str, value: Any) -> str:
        if key in self.translated:
            return gettext(str(value))
        if key in self.coded:
            return self._label(self.coded[key], str(value))
        return str(value)

    @staticmethod
    def _label(choices: type[models.TextChoices], value: str) -> str:
        """The label of a known value; an unknown one as it is."""
        try:
            return str(choices(value).label)
        except ValueError:
            return value


def english(label: Promise) -> str:
    """A panel word as the details of an event keep it, whatever language is active."""
    with translation.override("en"):
        return str(label)


EVENTS: dict[Event, EventType] = {
    Event.SERVER_DOWN: EventType(
        _("Server is down"),
        _("Server {server} is down"),
        _("The server {server} ({host}) does not answer. Last answer: {last_seen}."),
    ),
    Event.LIMIT_REACHED: EventType(
        _("Limit reached"),
        _("Limit reached: {user}"),
        _("The keys of {user} are turned off: {reason}."),
        to_user=True,
        coded={"reason": LimitReason},
    ),
    Event.ACCESS_EXPIRING: EventType(
        _("Access ends soon"),
        _("Access ends soon"),
        _("The VPN access of {user} ends on {until}."),
        to_user=True,
    ),
    Event.KEYS_CHANGED: EventType(
        _("Keys must be imported again"),
        _("New VPN settings on {server}"),
        _(
            "The settings of {server} have changed. Open your keys in the personal account "
            "and import them into the Amnezia app again: the old ones no longer connect."
        ),
        to_user=True,
    ),
    Event.KEYS_REVOKED: EventType(
        _("Keys revoked"),
        _("VPN keys on {server} are revoked"),
        _(
            "The VPN protocol is removed from {server}, and your keys on it no longer connect. "
            "Open the personal account to see the keys you have."
        ),
        to_user=True,
    ),
    Event.KEYS_MOVED: EventType(
        _("Keys moved to AmneziaWG 3.1"),
        _("New VPN keys on {server}"),
        _(
            "Your VPN keys on {server} are moved to AmneziaWG 3.1. Open the personal account "
            "and import the new keys into the Amnezia app: the old ones stop working soon."
        ),
        to_user=True,
    ),
    # A server joined a group behind a relay (docs/FALLBACK.md section 6): the key of the user
    # now carries the address of the relay, which a key already in the app cannot learn.
    Event.KEYS_UPDATED: EventType(
        _("Keys updated for a server group"),
        _("Your VPN keys on {server} are updated"),
        _(
            "{server} now works in a group of servers that keeps the VPN up when one of them is "
            "blocked. Open your keys in the personal account and import them into the Amnezia "
            "app again, once: the old ones may stop working."
        ),
        to_user=True,
    ),
    Event.REGISTRATION_PENDING: EventType(
        _("New registration request"),
        _("New registration request: {user}"),
        _("{user} ({email}) waits for approval."),
    ),
    # `number` in the title lets the staff answer by replying to the message in a bot.
    Event.TICKET_CREATED: EventType(
        _("New support request"),
        _("New request #{number} from {user}: {subject}"),
        _("{user} opened a request ({type}): {subject}"),
        translated=("type",),
    ),
    # A series of messages is one notification: `text` holds them all, one a line, and `count`
    # says how many there are.
    Event.CHAT_MESSAGE: EventType(
        _("New chat message"),
        _("Message from {user}, chat #{number}"),
        _("{user}: {text}"),
        joined=True,
    ),
    # Only to the user, when they neither answered nor read the answer for a while; the text of
    # the answer is not repeated, `url` leads to the thread: the chat or a request.
    Event.SUPPORT_ANSWER: EventType(
        _("Support answered"),
        _("New message from support"),
        _("There is a new message in your support chat."),
        to_user=True,
        routable=False,
        fallback=True,
    ),
    Event.REQUEST_REMINDER: EventType(
        _("Support answered a request"),
        _("New answer to your request"),
        _("There is a new answer to your request: {topic}."),
        to_user=True,
        routable=False,
        fallback=True,
    ),
    # The staff's answer itself, to the messenger the user wrote from (DESIGN.md 6.8).
    Event.CHAT_ANSWER: EventType(
        _("Support answered in a messenger"),
        _("Answer from support"),
        _("{text}\n\nWrite here to answer."),
        to_user=True,
        routable=False,
    ),
    Event.REQUEST_ANSWER: EventType(
        _("Request answered in a messenger"),
        _("Answer to your request: {topic}"),
        _("{text}\n\nTo answer, open My requests (/requests)."),
        to_user=True,
        routable=False,
    ),
    Event.REQUEST_STATUS: EventType(
        _("Request status changed"),
        _("Your request: {topic}"),
        _("The request is now {status}."),
        to_user=True,
        routable=False,
        translated=("status",),
    ),
    Event.INCIDENT_OPENED: EventType(
        _("Incident opened"),
        _("Incident on {server}"),
        _("Users report that {server} does not work: {reports} so far."),
    ),
    Event.INCIDENT_CLOSED: EventType(
        _("Incident closed"),
        _("Incident on {server} is closed"),
        _("The incident on {server} is closed."),
    ),
    # Only to the users who reported the server; the staff hear of `INCIDENT_CLOSED`.
    Event.INCIDENT_RESOLVED: EventType(
        _("Server works again"),
        _("{server} works again"),
        _("The problem with {server} you reported is fixed. Thank you for telling us."),
        to_user=True,
        routable=False,
    ),
    Event.SUGGESTION_CREATED: EventType(
        _("New article suggestion"),
        _("Article suggestion from {user}: {title}"),
        _("{user} suggested for the guides ({kind}): {title}"),
        translated=("kind",),
    ),
    # The decision, only to the author.
    Event.SUGGESTION_ACCEPTED: EventType(
        _("Article suggestion published"),
        _("Your suggestion is published: {title}"),
        _("Thank you! The staff published your suggestion in the guides: {title}"),
        to_user=True,
        routable=False,
    ),
    Event.SUGGESTION_REJECTED: EventType(
        _("Article suggestion declined"),
        _("Your suggestion is declined: {title}"),
        _("The staff declined your suggestion: {title}\nReason: {reason}"),
        to_user=True,
        routable=False,
    ),
    Event.PAYMENT_RECEIVED: EventType(
        _("Payment received"),
        _("Payment received: {user}"),
        _("{user} paid {amount} {currency} for the plan {plan} ({provider})."),
    ),
    Event.RELAY_SWITCHED: EventType(
        _("Relay switched"),
        _("{group}: traffic moved to {to}"),
        _(
            "The relay {relay} cannot reach the server {from}, so the clients of the group "
            "{group} go through {to} now. Their connection drops for a few seconds."
        ),
    ),
    Event.MEMBER_UNREACHABLE: EventType(
        _("Group server unreachable from the relay"),
        _("{server} is unreachable from the relay"),
        _(
            "The relay {relay} cannot reach the server {server} of the group {group}. "
            "The relay sends no clients to it until it answers again."
        ),
    ),
    Event.GROUP_UNREACHABLE: EventType(
        _("No group server reachable"),
        _("{group}: no server is reachable"),
        _(
            "The relay {relay} reaches no server of the group {group}. "
            "The clients of the group have no connection."
        ),
    ),
    Event.RELAY_DOWN: EventType(
        _("Relay does not answer"),
        _("Relay {relay} does not answer"),
        _(
            "The panel cannot read the state of the relay {relay} ({host}). Last reading: "
            "{last_seen}. The relay may still forward the clients on its own."
        ),
    ),
    Event.MEMBER_LAGGING: EventType(
        _("Group server lags behind"),
        _("{server} lags behind the group {group}"),
        _(
            "The server {server} has missed key changes of the group {group} since {since}. "
            "The relay skips it until the panel copies the group files onto it."
        ),
    ),
    Event.TEST: EventType(
        _("Test"),
        _("Test notification"),
        _("Notifications from Lamnezia reach the channel {channel}."),
        routable=False,
    ),
}


def routable_events() -> list[tuple[str, Promise]]:
    """Events an administrator can route to channels, as form choices."""
    return [(event.value, kind.label) for event, kind in EVENTS.items() if kind.routable]
