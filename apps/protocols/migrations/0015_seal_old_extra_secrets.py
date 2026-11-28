"""Additional proxy secrets left in plain in old operations and journal records are sealed.

Operations started before the secrets were encrypted keep them in `params["proxy"]` (install) or
`params["options"]` (settings change): they move to `additional_secrets_token`, encrypted, so a
retry still has them. The journal never needed them: they are dropped from its records.
"""

from django.db import migrations
from django.db.models import Q

from apps.ssh.encryption import SecretCipher

FIELD = "additional_secrets"
TOKEN = "additional_secrets_token"
SECTIONS = ("proxy", "options")


def _with_plain(field: str) -> Q:
    found = Q()
    for section in SECTIONS:
        found |= Q(**{f"{field}__{section}__has_key": FIELD})
    return found


def _pop_plain(data: dict) -> list[str] | None:
    """Takes the plain secrets out of `data`; None when it has none."""
    found = None
    for section in SECTIONS:
        options = data.get(section)
        if isinstance(options, dict) and FIELD in options:
            options = dict(options)
            secrets = options.pop(FIELD) or []
            found = secrets.split(",") if isinstance(secrets, str) else list(secrets)
            data[section] = options
    return found


def seal(apps, schema_editor):
    Operation = apps.get_model("operations", "Operation")
    AuditEvent = apps.get_model("audit", "AuditEvent")
    cipher = SecretCipher.from_settings()
    for operation in Operation.objects.filter(_with_plain("params")):
        params = dict(operation.params)
        secrets = _pop_plain(params)
        if secrets is not None and TOKEN not in params:
            params[TOKEN] = cipher.encrypt(",".join(secrets))
        Operation.objects.filter(pk=operation.pk).update(params=params)
    for event in AuditEvent.objects.filter(_with_plain("data")):
        data = dict(event.data)
        _pop_plain(data)
        AuditEvent.objects.filter(pk=event.pk).update(data=data)


class Migration(migrations.Migration):
    dependencies = [
        ("protocols", "0014_service_extra_secrets"),
        ("operations", "0001_initial"),
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seal, migrations.RunPython.noop),
    ]
