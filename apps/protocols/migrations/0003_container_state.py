from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('protocols', '0002_dns'),
    ]

    operations = [
        migrations.AddField(
            model_name='protocolinstance',
            name='container_logs',
            field=models.BooleanField(default=False, verbose_name='keep the container log'),
        ),
        migrations.AddField(
            model_name='protocolinstance',
            name='container_removed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='container removed at'),
        ),
    ]
