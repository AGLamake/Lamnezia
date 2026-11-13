from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def strip_at(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """A name saved as "@bot" made t.me links that open telegram.org instead of the bot."""
    SiteSettings = apps.get_model("core", "SiteSettings")
    for site in SiteSettings.objects.filter(telegram_bot_username__startswith="@"):
        site.telegram_bot_username = site.telegram_bot_username.lstrip("@")
        site.save(update_fields=["telegram_bot_username"])


class Migration(migrations.Migration):
    dependencies = [("core", "0004_bot_key_delivery")]

    operations = [migrations.RunPython(strip_at, migrations.RunPython.noop)]
