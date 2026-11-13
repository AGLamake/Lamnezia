from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0004_delivery_claimed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="sent_via",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="notification",
            name="skip_via",
            field=models.CharField(blank=True, max_length=16),
        ),
    ]
