"""A move of the panel to another machine and the states that keep the panel read-only meanwhile."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.operations.models import OperationStatus


class MoveState(models.TextChoices):
    CHECKING = "checking", _("checking the new machine")
    PREPARING = "preparing", _("preparing the new machine")
    READY = "ready", _("the copy on the new machine is ready")
    LEFT = "left", _("the panel moved away from here")
    ARRIVED = "arrived", _("the panel moved here")
    CANCELLED = "cancelled", _("cancelled")
    FAILED = "failed", _("failed")


class DomainStatus(models.TextChoices):
    """Where the A record of the panel domain points after the hand-over."""

    UNCHECKED = "unchecked", _("not checked yet")
    UNRESOLVED = "unresolved", _("has no A record")
    ELSEWHERE = "elsewhere", _("points to another machine")
    POINTED = "pointed", _("points to the new machine")


class PanelMove(models.Model):
    """One move; the latest move decides whether the panel accepts changes.

    A move starts CHECKING: the new machine is checked while the panel still accepts changes, and
    the panel freezes only once it fits. The archive taken for the move holds this row in
    PREPARING, so the copy on the new machine starts read-only too and accepts changes only after
    `arrive`.
    """

    FROZEN: ClassVar[frozenset[str]] = frozenset(
        {MoveState.PREPARING, MoveState.READY, MoveState.LEFT}
    )
    # The states of the preparation operation, which renews its heartbeat in both.
    PREPARATION: ClassVar[frozenset[str]] = frozenset({MoveState.CHECKING, MoveState.PREPARING})
    # After these states only the domain is left to switch to the new machine.
    HANDED_OVER: ClassVar[frozenset[str]] = frozenset({MoveState.LEFT, MoveState.ARRIVED})

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
    handed_over_at = models.DateTimeField(_("handed over at"), null=True, blank=True)
    # Filled by the background check of the domain (services/domain.py), never by a request.
    domain_status = models.CharField(
        _("domain"), max_length=16, choices=DomainStatus, default=DomainStatus.UNCHECKED
    )
    domain_addresses = models.CharField(_("addresses of the domain"), max_length=255, blank=True)
    # What each resolver answered: [{"resolver", "addresses", "reachable"}] (services/resolvers.py).
    domain_answers = models.JSONField(_("answers of the resolvers"), default=list, blank=True)
    domain_checked_at = models.DateTimeField(_("domain checked at"), null=True, blank=True)
    # The old copy stops its stack once the domain points to the new machine (services/shutdown.py).
    auto_stop = models.BooleanField(_("stop the old copy automatically"), default=True)
    stop_requested_at = models.DateTimeField(_("stop requested at"), null=True, blank=True)
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
    def under_way(self) -> bool:
        """Being checked or keeping the panel read-only: no other move starts meanwhile."""
        return self.state == MoveState.CHECKING or self.frozen

    @property
    def failure(self) -> str:
        """Why a failed move failed, with what to do: its operation's error."""
        if self.state != MoveState.FAILED or self.operation is None:
            return ""
        return f"{self.operation.error_message} {self.operation.error_hint}".strip()

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

    @property
    def partly_confirmed(self) -> bool:
        """A confirmation started and did not finish: the copy may already accept changes."""
        return self.state == MoveState.READY and self.confirm_started and not self.busy

    @property
    def can_abandon(self) -> bool:
        """The move can end without the new machine: only this panel's freeze is lifted."""
        return self.partly_confirmed

    @property
    def awaits_domain(self) -> bool:
        """Handed over, and the domain does not point to the new machine yet."""
        return self.state in self.HANDED_OVER and self.domain_status != DomainStatus.POINTED

    @property
    def can_switch_auto_stop(self) -> bool:
        """The old copy has not asked its host to stop yet; only it decides about its stack."""
        waiting = (MoveState.CHECKING, MoveState.PREPARING, MoveState.READY, MoveState.LEFT)
        return self.state in waiting and self.stop_requested_at is None

    @property
    def should_stop(self) -> bool:
        """The move is confirmed, the domain points to the new machine and the stop is wanted."""
        return (
            self.state == MoveState.LEFT
            and self.auto_stop
            and self.domain_status == DomainStatus.POINTED
            and self.stop_requested_at is None
        )

    def mark(self, state: MoveState) -> None:
        self.state = state
        fields = ["state", "updated_at"]
        if state in self.HANDED_OVER and self.handed_over_at is None:
            self.handed_over_at = timezone.now()
            fields.append("handed_over_at")
        self.save(update_fields=fields)

    def mark_from(self, expected: MoveState, state: MoveState) -> bool:
        """Change the state only if it is still `expected`: another worker may have moved on."""
        changed = PanelMove.objects.filter(pk=self.pk, state=expected).update(
            state=state, updated_at=timezone.now()
        )
        if changed:
            self.state = state
        return bool(changed)
