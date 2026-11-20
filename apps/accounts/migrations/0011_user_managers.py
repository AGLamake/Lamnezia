"""Several staff members per user: `User.manager` becomes one manual `Management` row."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def to_managements(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Management = apps.get_model("accounts", "Management")
    served = User.objects.exclude(manager=None).values_list("pk", "manager_id")
    Management.objects.bulk_create(
        Management(user_id=user, member_id=member, via_plan=False) for user, member in served
    )


def to_manager(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Management = apps.get_model("accounts", "Management")
    for link in Management.objects.order_by("via_plan", "pk"):
        User.objects.filter(pk=link.user_id, manager=None).update(manager=link.member_id)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_sign_in_alerts"),
    ]

    operations = [
        migrations.CreateModel(
            name="Management",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("via_plan", models.BooleanField(default=False, verbose_name="from the plan")),
                (
                    "member",
                    models.ForeignKey(
                        limit_choices_to={"role": "moderator"},
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="staff member",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="managements",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="user",
                    ),
                ),
            ],
            options={
                "verbose_name": "staff member of the user",
                "verbose_name_plural": "staff members of the user",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "member"), name="accounts_management_unique"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("user", models.F("member")), _negated=True),
                        name="accounts_management_not_self",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="user",
            name="managers",
            field=models.ManyToManyField(
                blank=True,
                related_name="managed_users",
                through="accounts.Management",
                through_fields=("user", "member"),
                to=settings.AUTH_USER_MODEL,
                verbose_name="staff members",
            ),
        ),
        migrations.RunPython(to_managements, to_manager),
        migrations.RemoveField(
            model_name="user",
            name="manager",
        ),
    ]
