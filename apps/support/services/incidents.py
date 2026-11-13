"""Incidents (DESIGN.md 6.7): "server does not work" reports of one server, grouped so the staff
answer them once and the status page shows the server as having problems meanwhile.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q, QuerySet
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services.zone import Zone
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.support.models import OPEN_STATUSES, Conversation, Incident, TicketEvent, TicketStatus
from apps.support.services import live
from apps.support.services.chat import MyChat
from apps.support.services.relay import last_via, tell_of_status
from apps.support.services.thread import post


def incidents() -> QuerySet[Incident]:
    """Incidents with the number of reports in `reports`, open ones first."""
    return (
        Incident.objects.select_related("server", "closed_by")
        .annotate(reports=Count("conversations"))
        .order_by(F("closed_at").desc(nulls_first=True), "-opened_at")
    )


def servers_with_open_incident() -> set[int]:
    return set(Incident.objects.filter(closed_at=None).values_list("server_id", flat=True))


def add_report(conversation: Conversation) -> Incident:
    """Put a report into the open incident of its server, or open one.

    A report joins an incident whose last report came less than SUPPORT_INCIDENT_WINDOW_MINUTES
    ago; a later one opens a new incident.
    """
    assert conversation.server is not None
    now = timezone.now()
    since = now - timedelta(minutes=settings.SUPPORT_INCIDENT_WINDOW_MINUTES)
    with transaction.atomic():
        incident = (
            Incident.objects.select_for_update()
            .filter(server=conversation.server, closed_at=None, last_report_at__gte=since)
            .order_by("-last_report_at")
            .first()
        )
        if incident is None:
            incident = Incident.objects.create(server=conversation.server)
            emit(
                Event.INCIDENT_OPENED,
                {"server": conversation.server.name, "reports": 1},
                key=f"incident-{incident.pk}-opened",
            )
        else:
            incident.last_report_at = now
            incident.save(update_fields=["last_report_at"])
        conversation.incident = incident
        conversation.save(update_fields=["incident"])
    return incident


def take_incident(incident: Incident, by: User) -> int:
    """The staff member takes the incident up: its open requests are assigned to them, and new
    ones go into work, so each reporter hears that someone is on it. Returns how many.

    A moderator takes only the requests of their zone that nobody else has, as their inbox
    shows them.
    """
    requests = incident.conversations.filter(status__in=OPEN_STATUSES)
    if not by.is_admin:
        requests = Zone(by).narrow(requests, user="user").filter(Q(assignee=by) | Q(assignee=None))
    taken = 0
    with transaction.atomic():
        for conversation in requests:
            started = conversation.status == TicketStatus.NEW
            conversation.assignee = by
            if started:
                conversation.status = TicketStatus.IN_PROGRESS
            conversation.save(update_fields=["assignee", "status"])
            live.thread_changed(conversation)
            live.unread_changed(conversation, staff=True)
            if started:
                tell_of_status(conversation)
            taken += 1
    return taken


def takers(incident: Incident) -> list[str]:
    """Who has the incident's open requests: the staff members who took it."""
    names = (
        incident.conversations.filter(status__in=OPEN_STATUSES, assignee__isnull=False)
        .values_list("assignee__username", flat=True)
        .distinct()
    )
    return sorted(names)


def close_incident(incident: Incident, by: User, text: str = "") -> None:
    """Close the incident and every open request in it; each reporter hears the server works,
    and their chat gets a line about the request.

    `text`, if given, goes to every request as the staff's answer.
    """
    server = incident.server.name
    with transaction.atomic():
        incident.closed_at = timezone.now()
        incident.closed_by = by
        incident.save(update_fields=["closed_at", "closed_by"])
        requests = incident.conversations.filter(status__in=OPEN_STATUSES).select_related("user")
        for conversation in requests:
            chat = MyChat(conversation.user)
            if text.strip():
                post(conversation, by, text)
                chat.tell(conversation, TicketEvent.ANSWER)
            conversation.status = TicketStatus.CLOSED
            conversation.save(update_fields=["status"])
            chat.tell(conversation, TicketEvent.CLOSED)
            emit(
                Event.INCIDENT_RESOLVED,
                {"server": server},
                key=f"incident-{incident.pk}-user-{conversation.user_id}",
                user=conversation.user,
                via=last_via(conversation),
            )
        emit(Event.INCIDENT_CLOSED, {"server": server}, key=f"incident-{incident.pk}-closed")
