from django.db import migrations


def superusers_to_admins(apps, schema_editor):
    """Accounts made before roles existed keep their rights: superusers become administrators."""
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_superuser=True).update(role="admin")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_user_role"),
    ]

    operations = [
        migrations.RunPython(superusers_to_admins, migrations.RunPython.noop),
    ]
