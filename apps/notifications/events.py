"""The events the panel notifies about and how each reads in a message.

Event details (`data`) are flat JSON with the keys the texts use; a webhook gets them as they are,
so a key, once released, is never renamed (docs/wiki: Notifications). A list detail reads as its
items joined, an empty list or null as a dash.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from django.db import models
from django.template.defaultfilters import filesizeformat
from django.utils import translation
from django.utils.functional import Promise
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.core.models import ReportFrequency
from apps.groups.models import LagReason
from apps.servers.models import LoadResource
from apps.traffic.signals import LimitReason


class Event(StrEnum):
    SERVER_DOWN = "server_down"
    SERVER_UP = "server_up"
    AWG3_DROPPED = "awg3_dropped"
    AWG3_RESTORED = "awg3_restored"
    SERVER_LOAD_HIGH = "server_load_high"
    SERVER_LOAD_NORMAL = "server_load_normal"
    LIMIT_REACHED = "limit_reached"
    ACCESS_EXPIRING = "access_expiring"
    TRAFFIC_LOW = "traffic_low"
    KEYS_CHANGED = "keys_changed"
    KEYS_REVOKED = "keys_revoked"
    KEYS_MOVED = "keys_moved"
    SHORT_ID_CHANGED = "short_id_changed"
    KEYS_UPDATED = "keys_updated"
    REGISTRATION_PENDING = "registration_pending"
    SIGN_IN_NEW_PLACE = "sign_in_new_place"
    SIGN_IN_LOCKED = "sign_in_locked"
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
    MEMBER_REACHABLE = "member_reachable"
    GROUP_REACHABLE = "group_reachable"
    RELAY_UP = "relay_up"
    MEMBER_CAUGHT_UP = "member_caught_up"
    SPLIT_LIST_STALE = "split_list_stale"
    STAFF_REPORT = "staff_report"
    ZONE_REPORT = "zone_report"
    TEST = "test"


@dataclass(frozen=True)
class EventType:
    """`to_user`: the user the event is about hears of it too, besides the routed channels.

    `translated`: details that hold panel words in English (`english`), shown in the reader's
    language. `coded`: details that hold a choice value, which a webhook gets as it is and a
    message shows by its label. `joined`: occurrences about one user close in time make one
    notification. `fallback`: when the user's channel does not take it, their other channels
    are tried in turn. `link_offer`: a letter to the user offers to link a messenger.
    `browser`: the browsers the user subscribed get it too (only with `to_user`). `sizes`:
    details in bytes, shown as a size; a list of [name, bytes] pairs reads as "name (size)".
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
    link_offer: bool = False
    browser: bool = True
    sizes: tuple[str, ...] = ()

    def render(self, data: dict[str, Any]) -> tuple[str, str]:
        """Title and text in the active language with the details filled in; a missing detail
        reads as a dash.
        """
        values = defaultdict(lambda: "—", {key: self._value(key, v) for key, v in data.items()})
        text = str(self.text).format_map(values).rstrip()
        if data.get("url"):
            text = f"{text}\n{data['url']}"
        return str(self.title).format_map(values), text

    def _value(self, key: str, value: Any) -> str:
        if value is None or value == []:
            return "—"
        if key in self.sizes:
            return self._size(value)
        if isinstance(value, list):
            return ", ".join(map(str, value))
        if key in self.translated:
            # An empty detail stays empty: gettext("") is the header of the catalog.
            return gettext(str(value)) if value else ""
        if key in self.coded:
            return self._label(self.coded[key], str(value))
        return str(value)

    @staticmethod
    def _size(value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(f"{name} ({filesizeformat(size)})" for name, size in value)
        return str(filesizeformat(value))

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
    Event.SERVER_UP: EventType(
        _("Server answers again"),
        _("Server {server} answers again"),
        _("The server {server} ({host}) answers again. Answer before the outage: {last_seen}."),
    ),
    Event.AWG3_DROPPED: EventType(
        _("AmneziaWG runs without the 3.1 settings"),
        _("AmneziaWG on {server} runs without the 3.1 settings"),
        _(
            "The amneziawg kernel module on {server} ({host}) is older than 3.1 and dropped the "
            "AmneziaWG 3.1 settings: clients with them do not connect. Update or remove the "
            "amneziawg module on the host and restart amnezia-awg2."
        ),
    ),
    Event.AWG3_RESTORED: EventType(
        _("AmneziaWG runs with the 3.1 settings again"),
        _("AmneziaWG on {server} runs with the 3.1 settings again"),
        _(
            "amnezia-awg2 on {server} ({host}) takes the AmneziaWG 3.1 settings again. "
            "Dropped since {since}."
        ),
    ),
    # `resource` is cpu, memory or disk; `percent` is the latest reading, `since` the first
    # reading of the run above the threshold. While the load lasts, the alert is repeated.
    Event.SERVER_LOAD_HIGH: EventType(
        _("Server load is high"),
        _("High load on {server}: {resource} {percent}%"),
        _(
            "{resource} of the server {server} ({host}) is at {percent}%, at or above the "
            "threshold of {threshold}%, since {since}."
        ),
        coded={"resource": LoadResource},
    ),
    Event.SERVER_LOAD_NORMAL: EventType(
        _("Server load is back to normal"),
        _("Load on {server} is back to normal: {resource}"),
        _(
            "{resource} of the server {server} ({host}) is at {percent}%, below the threshold "
            "of {threshold}%. High since {since}."
        ),
        coded={"resource": LoadResource},
    ),
    Event.LIMIT_REACHED: EventType(
        _("Limit reached"),
        _("Limit reached: {user}"),
        _("The keys of {user} are turned off: {reason}."),
        to_user=True,
        coded={"reason": LimitReason},
        link_offer=True,
    ),
    Event.ACCESS_EXPIRING: EventType(
        _("Access ends soon"),
        _("Access ends soon"),
        _("The VPN access of {user} ends on {until}."),
        to_user=True,
        link_offer=True,
    ),
    # `limit` names which limit: of the period or of the whole plan. `note` is empty, or says
    # that traffic of some keys is not counted now and the real share may be higher.
    Event.TRAFFIC_LOW: EventType(
        _("Traffic runs out soon"),
        # xgettext:no-python-format
        _("{percent}% of the traffic is used"),
        # xgettext:no-python-format
        _("{user} has used {percent}% of the traffic limit {limit}.\n{note}"),
        to_user=True,
        translated=("limit", "note"),
        link_offer=True,
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
    Event.SHORT_ID_CHANGED: EventType(
        _("XRay keys must be imported again"),
        _("New XRay settings on {server}"),
        _(
            "The XRay settings of {server} have changed. Open your keys in the personal account "
            "and import them into the Amnezia app again: the ones added before no longer "
            "connect. A subscription link picks the change up by itself."
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
    # Only to the user: the panel was signed in to from a network or a browser it was not signed
    # in to from before. `address` is left out when unknown, `url` leads to the sessions.
    Event.SIGN_IN_NEW_PLACE: EventType(
        _("Sign-in from a new place"),
        _("New sign-in to your account"),
        _(
            "{user} has signed in to the panel from a new network or browser.\n"
            "Browser: {device}\nAddress: {address}\n"
            "Not you? Open the sessions, end the one you do not know and change the password."
        ),
        to_user=True,
        routable=False,
    ),
    # The password is never among the details: only the name typed in and the address.
    Event.SIGN_IN_LOCKED: EventType(
        _("Sign-in locked after wrong passwords"),
        _("Sign-in locked: {user}"),
        _(
            "After {failures} wrong passwords, sign-in as {user} from {address} is locked "
            "for {minutes} minutes."
        ),
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
        # The answer itself goes to the messenger; a push would repeat it.
        browser=False,
    ),
    Event.REQUEST_ANSWER: EventType(
        _("Request answered in a messenger"),
        _("Answer to your request: {topic}"),
        _("{text}\n\nTo answer, open My requests (/requests)."),
        to_user=True,
        routable=False,
        # The answer itself goes to the messenger; a push would repeat it.
        browser=False,
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
            "The server {server} has missed key changes of the group {group} since {since}: "
            "{reason}. The relay skips it until the panel copies the group files onto it."
        ),
        coded={"reason": LagReason},
    ),
    Event.MEMBER_REACHABLE: EventType(
        _("Group server reachable from the relay again"),
        _("{server} is reachable from the relay again"),
        _(
            "The relay {relay} reaches the server {server} of the group {group} again "
            "and may send clients to it."
        ),
    ),
    Event.GROUP_REACHABLE: EventType(
        _("Group server reachable again"),
        _("{group}: a server is reachable again"),
        _(
            "The relay {relay} reaches a server of the group {group} again. "
            "The clients of the group have a connection."
        ),
    ),
    Event.RELAY_UP: EventType(
        _("Relay answers again"),
        _("Relay {relay} answers again"),
        _(
            "The panel reads the state of the relay {relay} ({host}) again. "
            "Reading before the outage: {last_seen}."
        ),
    ),
    Event.MEMBER_CAUGHT_UP: EventType(
        _("Group server caught up"),
        _("{server} caught up with the group {group}"),
        _(
            "The server {server} has the files of the group {group} again after lagging behind "
            "since {since}. The relay sends clients to it again."
        ),
    ),
    # Once a day while a source of a split group's network list fails (docs/SPLIT.md 4.5): the
    # group keeps its list. `source` is the host of the address, `days` count from the last
    # good list.
    Event.SPLIT_LIST_STALE: EventType(
        _("Network list not updated"),
        _("{group}: network list not updated for {days} days"),
        _(
            "The list from {source} for the split group {group} was not taken: {error}. "
            "The relay keeps the list it has."
        ),
    ),
    # The summary of `SiteSettings.report_frequency` for the channels: the whole panel since
    # `since`. Lists name servers, relays, groups and keys, `access_ending` reads "user date";
    # `backups_oldest` and `archive_at` are null when there is no copy. `url` leads to the
    # dashboard.
    Event.STAFF_REPORT: EventType(
        _("Report to the staff"),
        _("Panel report since {since}"),
        _(
            "Servers down: {servers_down}\n"
            "Relays not read: {relays_down}\n"
            "Groups with no member answering: {groups_down}\n"
            "Traffic: {traffic}\n"
            "Most active keys: {top_keys}\n"
            "Access ends within a week: {access_ending}\n"
            "Failed operations: {failed_operations}\n"
            "Oldest protocol backup: {backups_oldest}\n"
            "Latest panel archive: {archive_at}\n"
            "Applications waiting: {applications}\n"
            "New support requests: {new_requests}"
        ),
        coded={"period": ReportFrequency},
        sizes=("traffic", "top_keys"),
    ),
    # Only to a moderator: the same summary within their zone, without what is an
    # administrator's (relays, groups, the panel archive, applications).
    Event.ZONE_REPORT: EventType(
        _("Report to a moderator"),
        _("Report on your zone since {since}"),
        _(
            "Servers down: {servers_down}\n"
            "Traffic: {traffic}\n"
            "Most active keys: {top_keys}\n"
            "Access ends within a week: {access_ending}\n"
            "Failed operations: {failed_operations}\n"
            "Oldest protocol backup: {backups_oldest}\n"
            "New support requests: {new_requests}"
        ),
        to_user=True,
        routable=False,
        browser=False,
        coded={"period": ReportFrequency},
        sizes=("traffic", "top_keys"),
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
