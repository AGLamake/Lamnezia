"""Readable names of audit journal actions: `action_label("keys.issue")` → "Issue key".

Actions that start a background operation read as the operation's title; the rest are named here.
An action no one names reads as its code.
"""

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.operations.services.registry import operations

_LABELS: dict[str, Promise] = {
    "accounts.approve": _("Approve registration"),
    "accounts.reject": _("Reject registration"),
    "accounts.backup_codes_issue": _("Issue backup codes"),
    "accounts.backup_code_use": _("Sign in with a backup code"),
    "accounts.invite_create": _("Create invitation"),
    "accounts.invite_delete": _("Delete invitation"),
    "accounts.link": _("Link messenger"),
    "accounts.unlink": _("Unlink messenger"),
    "accounts.link_code": _("Issue messenger link code"),
    "accounts.messenger_login": _("Sign in from a messenger"),
    "accounts.email_change": _("Change email"),
    "accounts.sessions_end": _("End sessions"),
    "billing.checkout": _("Start payment of plan"),
    "billing.paid": _("Receive payment"),
    "billing.refunded": _("Refund payment"),
    "bots.command": _("Bot command"),
    "bots.turn_on": _("Turn bot on"),
    "bots.turn_off": _("Turn bot off"),
    "bots.restart": _("Restart bot"),
    "bots.leave": _("Bot leaves server"),
    "core.settings_change": _("Change site settings"),
    "groups.create": _("Create server group"),
    "groups.delete": _("Delete server group"),
    "guides.suggestion_accept": _("Publish article suggestion"),
    "guides.suggestion_reject": _("Decline article suggestion"),
    "keys.view": _("Show key"),
    "keys.reset_view": _("Allow showing key again"),
    "keys.subscription_renew": _("Issue subscription link"),
    "keys.subscription_delete": _("Delete subscription link"),
    "notifications.channel_create": _("Create notification channel"),
    "notifications.channel_update": _("Change notification channel"),
    "notifications.channel_delete": _("Delete notification channel"),
    "plans.create": _("Create plan"),
    "plans.update": _("Edit plan"),
    "plans.delete": _("Delete plan"),
    "plans.limits": _("Change user limits"),
    "protocols.readdress": _("Move keys to the new server address"),
    "protocols.service_password_view": _("Show SFTP password"),
    "servers.forget_host_key": _("Forget server host key"),
    "support.template_create": _("Create reply template"),
    "support.template_update": _("Edit reply template"),
    "support.template_delete": _("Delete reply template"),
    "users.extend": _("Extend access of users"),
    "users.disable": _("Disable keys of users"),
    "users.change_plan": _("Change plan of users"),
}


def action_label(action: str) -> str:
    """The action's name in the active language; its code if nothing names it."""
    label = _LABELS.get(action)
    return str(label) if label is not None else operations.title(action)
