"""A move of the panel to another machine and the states that keep the panel read-only meanwhile."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.operations.models import OperationStatus


class MoveState(models.TextChoices):
    PREPARING = "preparing", _("preparing the new machine")
    READY = "ready", _("the copy on the new machine is ready")
    LEFT = "left", _("the panel moved away from here")
    ARRIVED = "arrived", _("the panel moved here")
    CANCELLED = "cancelled", _("cancelled")
    FAILED = "failed", _("failed")


class PanelMove(models.Model):
    """One move; the latest move decides whether the panel accepts changes.

    The archive taken for the move holds this row in PREPARING, so the copy on the new machine
    starts read-only too and accepts changes only after `arrive`.
    """

    FROZEN: ClassVar[frozenset[str]] = frozenset(
        {MoveState.PREPARING, MoveState.READY, MoveState.LEFT}
    )

    target = models.ForeignKey(
        "servers.Server",
        verbose_name=_("new machine"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    # The address the admin points the panel domain to; kept if the server row goes.
    target_host = models.CharField(_("address of the new machine"), max_length=255)
    state = models.CharField(
        _("state"), max_length=16, choices=MoveState, default=MoveState.PREPARING
    )
    # The latest operation of the move: preparing, confirming or cancelling.
    operation = models.ForeignKey(
        "operations.Operation",
        verbose_name=_("operation"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Set once a confirmation started: the copy may already accept changes, so it is not
    # removed blindly any more.
    confirm_started = models.BooleanField(_("confirmation started"), default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("started by"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    created_at = models.DateTimeField(_("started at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("panel move")
        verbose_name_plural = _("panel moves")
        ordering: ClassVar[list[str]] = ["-pk"]

    def __str__(self) -> str:
        return f"{self.target_host} ({self.get_state_display()})"

    @classmethod
    def latest(cls) -> "PanelMove | None":
        return cls.objects.order_by("-pk").first()

    @property
    def frozen(self) -> bool:
        return self.state in self.FROZEN

    @property
    def busy(self) -> bool:
        """An operation of the move is queued or running."""
        status = self.operation.status if self.operation else None
        return status in (OperationStatus.PENDING, OperationStatus.RUNNING)

    @property
    def can_confirm(self) -> bool:
        return self.state == MoveState.READY and not self.busy

    @property
    def can_cancel(self) -> bool:
        cancellable = (MoveState.PREPARING, MoveState.READY, MoveState.FAILED)
        return self.state in cancellable and not self.busy

    def mark(self, state: MoveState) -> None:
        self.state = state
        self.save(update_fields=["state", "updated_at"])
