"""Writing the audit journal: ``audit.log("keys.issue", actor=user, target=key, ip=ip)``."""

import ipaddress
from collections.abc import Mapping
from typing import Any

from django.db import models
from django.http import HttpRequest

from apps.accounts.models import User
from apps.audit.models import AuditEvent

_TARGET_REPR_LIMIT = 200


def log(
    action: str,
    *,
    actor: User | None,
    target: models.Model | None = None,
    data: Mapping[str, Any] | None = None,
    ip: str | None = None,
) -> AuditEvent:
    """Record an action. `action` is "<app>.<verb>"; `data` must never hold secrets."""
    return AuditEvent.objects.create(
        actor=actor,
        actor_name=actor.get_username() if actor else "",
        action=action,
        target_type=target._meta.label_lower if target is not None else "",
        target_id=str(target.pk) if target is not None else "",
        target_repr=str(target)[:_TARGET_REPR_LIMIT] if target is not None else "",
        data=dict(data or {}),
        ip=ip,
    )


def log_request(
    request: HttpRequest, action: str, target: models.Model | None = None, **data: Any
) -> AuditEvent:
    """Record an action of the visitor who sent `request`, with their address."""
    actor = request.user if isinstance(request.user, User) else None
    return log(action, actor=actor, target=target, data=data, ip=client_ip(request))


def client_ip(request: HttpRequest) -> str | None:
    """The visitor address; django-axes uses it too, so lockouts and the journal agree.

    Only the last X-Forwarded-For entry is trusted: nginx and Caddy in front of the panel add it
    themselves, while the entries before it come from the client and can be anything. The header
    from anyone but a trusted proxy is already dropped by config.security.TrustedProxyMiddleware.
    """
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR", "")).rsplit(",", 1)[-1].strip()
    if _is_ip(forwarded):
        return forwarded
    peer = request.META.get("REMOTE_ADDR")
    return peer if isinstance(peer, str) and _is_ip(peer) else None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
