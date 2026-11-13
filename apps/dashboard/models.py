from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class BatchAction(models.TextChoices):
    EXTEND = "extend", _("Extend access")
    DISABLE = "disable", _("Disable keys")
    CHANGE_PLAN = "change_plan", _("Change plan")
    REISSUE = "reissue", _("Reissue keys")


class UserBatch(models.Model):
    """A bulk action on users, kept for its report: the outcome for each user and the key
    operations it started. `days` belongs to an extension, `plan` to a change of plan."""

    action = models.CharField(_("action"), max_length=16, choices=BatchAction.choices)
    days = models.PositiveSmallIntegerField(_("days"), null=True, blank=True)
    plan = models.ForeignKey(
        "plans.Plan",
        verbose_name=_("plan"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("started by"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("bulk action")
        verbose_name_plural = _("bulk actions")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.get_action_display()} #{self.pk}"


class Outcome(models.TextChoices):
    DONE = "done", _("done")
    SKIPPED = "skipped", _("skipped")


class UserBatchItem(models.Model):
    """What a bulk action did to one user. The note is in the language of the administrator."""

    batch = models.ForeignKey(
        UserBatch, verbose_name=_("bulk action"), on_delete=models.CASCADE, related_name="items"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("user"), on_delete=models.CASCADE, related_name="+"
    )
    outcome = models.CharField(_("outcome"), max_length=16, choices=Outcome.choices)
    note = models.CharField(_("note"), max_length=200, blank=True)
    operations = models.ManyToManyField(
        "operations.Operation", verbose_name=_("operations"), blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("bulk action result")
        verbose_name_plural = _("bulk action results")
        ordering = ("pk",)

    def __str__(self) -> str:
        return f"{self.batch} · {self.user}"
