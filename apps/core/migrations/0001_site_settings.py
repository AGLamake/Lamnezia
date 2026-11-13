
import apps.ssh.encryption
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='SiteSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('site_name', models.CharField(default='Lamnezia', max_length=64, verbose_name='panel name')),
                ('registration_mode', models.CharField(choices=[('invite_only', 'By invitation only'), ('open_auto', 'Open, the default plan right away'), ('open_manual', 'Open, an administrator approves')], default='invite_only', max_length=16, verbose_name='registration')),
                ('require_email_confirmation', models.BooleanField(default=True, verbose_name='confirm email on registration')),
                ('key_reveal_policy', models.CharField(choices=[('always', 'Any time'), ('once', 'Only once')], default='always', max_length=16, verbose_name='showing a key again')),
                ('smtp_host', models.CharField(blank=True, max_length=255, verbose_name='SMTP server')),
                ('smtp_port', models.PositiveIntegerField(default=587, verbose_name='SMTP port')),
                ('smtp_security', models.CharField(choices=[('starttls', 'STARTTLS'), ('ssl', 'SSL/TLS'), ('none', 'None')], default='starttls', max_length=16, verbose_name='encryption')),
                ('smtp_username', models.CharField(blank=True, max_length=255, verbose_name='SMTP user')),
                ('smtp_password', apps.ssh.encryption.EncryptedTextField(blank=True, verbose_name='SMTP password')),
                ('from_email', models.CharField(blank=True, max_length=255, verbose_name='sender')),
                ('telegram_bot_token', apps.ssh.encryption.EncryptedTextField(blank=True, verbose_name='Telegram bot token')),
                ('github_token', apps.ssh.encryption.EncryptedTextField(blank=True, verbose_name='GitHub token')),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'panel settings',
                'verbose_name_plural': 'panel settings',
            },
        ),
    ]
