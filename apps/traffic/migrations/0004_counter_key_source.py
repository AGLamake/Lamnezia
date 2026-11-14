from django.db import migrations, models
from django.db.models import F


def same_as_source(apps, schema_editor):
    counters = apps.get_model("traffic", "TrafficCounter")
    counters.objects.update(key_source=F("source"))


class Migration(migrations.Migration):

    dependencies = [
        ('traffic', '0003_online_and_restarts'),
    ]

    operations = [
        migrations.AddField(
            model_name='trafficcounter',
            name='key_source',
            field=models.CharField(default='', max_length=100, verbose_name='source of the key'),
            preserve_default=False,
        ),
        migrations.RunPython(same_as_source, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='trafficcounter',
            index=models.Index(fields=['key_source', 'public_key'], name='traffic_tra_key_sou_1a81ed_idx'),
        ),
    ]
