from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class OperationStatus(models.TextChoices):
    PENDING = "pending", _("waiting")
    RUNNING = "running", _("running")
    SUCCEEDED = "succeeded", _("done")
    FAILED = "failed", _("failed")


class OperationQuerySet(models.QuerySet["Operation"]):
    def newest_first(self) -> "OperationQuerySet":
        return self.select_related("created_by").order_by("-created_at")


class Operation(models.Model):
    """One background job (usually on a server over SSH) with its steps, result and error.

    `params` are the job inputs, enough to run it again; they hold ids, not secrets. A secret the
    job cannot do without goes in encrypted (SecretCipher), under a key from SECRET_PARAMS, so
    `rotate_encryption_key` re-encrypts it too and a retry after the rotation still reads it.
    """

    SECRET_PARAMS = ("password_token",)

    kind = models.CharField(_("kind"), max_length=100)
    subject = models.CharField(_("subject"), max_length=200, blank=True)
    params = models.JSONField(_("parameters"), default=dict)
    status = models.CharField(
        _("status"), max_length=16, choices=OperationStatus, default=OperationStatus.PENDING
    )
    # Steps and errors are written in the language of the person who started the operation.
    language = models.CharField(_("language"), max_length=16)
    error_code = models.CharField(_("error code"), max_length=64, blank=True)
    error_message = models.TextField(_("error"), blank=True)
    error_hint = models.TextField(_("what to do"), blank=True)
    retry_of = models.ForeignKey(
        "self",
        verbose_name=_("retry of"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="retries",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("started by"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    objects = OperationQuerySet.as_manager()

    class Meta:
        verbose_name = _("operation")
        verbose_name_plural = _("operations")
        indexes = (models.Index(fields=["status", "created_at"]),)

    def __str__(self) -> str:
        return f"{self.kind} #{self.pk}"

    @property
    def is_finished(self) -> bool:
        return self.status in (OperationStatus.SUCCEEDED, OperationStatus.FAILED)

    @property
    def can_retry(self) -> bool:
        return self.status == OperationStatus.FAILED

    def finish(
        self, status: OperationStatus, *, code: str = "", message: str = "", hint: str = ""
    ) -> None:
        self.status = status
        self.error_code, self.error_message, self.error_hint = code, message, hint
        self.finished_at = timezone.now()
        self.save(
            update_fields=["status", "error_code", "error_message", "error_hint", "finished_at"]
        )


class StepStatus(models.TextChoices):
    RUNNING = "running", _("running")
    SUCCEEDED = "succeeded", _("done")
    FAILED = "failed", _("failed")


class OperationStep(models.Model):
    """A named stage of an operation, e.g. «Connect to the server», with its log."""

    operation = models.ForeignKey(
        Operation, verbose_name=_("operation"), on_delete=models.CASCADE, related_name="steps"
    )
    position = models.PositiveSmallIntegerField(_("position"))
    title = models.CharField(_("title"), max_length=200)
    status = models.CharField(
        _("status"), max_length=16, choices=StepStatus, default=StepStatus.RUNNING
    )
    log = models.TextField(_("log"), blank=True)
    started_at = models.DateTimeField(_("started at"), auto_now_add=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    class Meta:
        verbose_name = _("operation step")
        verbose_name_plural = _("operation steps")
        ordering = ("position",)
        constraints = (
            models.UniqueConstraint(
                fields=["operation", "position"], name="operations_step_unique_position"
            ),
        )

    def __str__(self) -> str:
        return self.title
