"""The «Staff» section: the list of staff members, a new member, their rights and links, and the
«Staff» block on the pages of a server, a plan and a user.

Only members of the actor's zone exist here: anyone else, or a server, plan or user outside the
zone, answers 404. The rules themselves are in `StaffDesk`.
"""

from dataclasses import dataclass
from functools import cached_property
from typing import Any

from django.contrib import messages
from django.db.models import Count, Model, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import FormView, TemplateView

from apps.accounts.forms_staff import StaffCreateForm, StaffForm, StaffPickForm, StaffRoleForm
from apps.accounts.models import Management, User
from apps.accounts.permissions import SectionRequiredMixin
from apps.accounts.services.access import NotAllowedError, Section
from apps.accounts.services.staff import StaffChange, StaffDesk
from apps.plans.models import Plan
from apps.servers.models import Server


@dataclass(frozen=True)
class StaffRow:
    member: User
    via_plan: bool = False


@dataclass(frozen=True)
class StaffBlock:
    """The staff of a server, a plan or a user as the actor sees them, with a form to add one.

    `kind` is the part of the URL that names the object: ``server``, ``plan`` or ``user``.
    """

    kind: str
    pk: int
    rows: list[StaffRow]
    form: StaffPickForm | None

    @classmethod
    def of(cls, member: User, obj: Server | Plan | User) -> "StaffBlock | None":
        """None when the actor does not manage the staff."""
        desk = StaffDesk(member)
        if not desk.can_manage:
            return None
        mine = desk.members()
        if isinstance(obj, User):
            links = Management.objects.filter(user=obj, member__in=mine).select_related("member")
            rows = [StaffRow(link.member, link.via_plan) for link in links.order_by("member")]
            assigned = User.objects.filter(pk__in=[row.member.pk for row in rows] + [obj.pk])
        else:
            assigned = obj.moderators.all() if isinstance(obj, Server) else obj.staff.all()
            rows = [StaffRow(staff) for staff in mine.filter(pk__in=assigned.values("pk"))]
        candidates = desk.candidates_for(assigned)
        form = StaffPickForm(candidates=candidates) if candidates.exists() else None
        return cls(_KINDS[type(obj)], obj.pk, rows, form)

    @property
    def add_url(self) -> str:
        return reverse("accounts:staff-block-add", args=[self.kind, self.pk])

    @property
    def remove_url(self) -> str:
        return reverse("accounts:staff-block-remove", args=[self.kind, self.pk])


_KINDS: dict[type[Model], str] = {Server: "server", Plan: "plan", User: "user"}


class _StaffView(SectionRequiredMixin, View):
    section = Section.STAFF

    @property
    def desk(self) -> StaffDesk:
        return StaffDesk(self.member, self.client_ip)

    def target(self) -> User:
        return get_object_or_404(self.desk.members(), pk=self.kwargs["pk"])


class StaffListView(_StaffView, TemplateView):
    template_name = "accounts/staff/list.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        members = self.desk.members().annotate(
            served=Count("managed_users", distinct=True),
            server_count=Count("moderated_servers", distinct=True),
        )
        return super().get_context_data(members=members, **kwargs)


class StaffCreateView(_StaffView, FormView):  # type: ignore[type-arg]
    template_name = "accounts/staff/create.html"
    form_class = StaffCreateForm

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "desk": self.desk}

    def form_valid(self, form: StaffCreateForm) -> HttpResponse:
        data = form.cleaned_data
        member = self.desk.create(
            data["username"],
            data["email"],
            data["password1"],
            data["role"],
            temporary=data["must_change_password"],
        )
        messages.success(self.request, _("The staff member is added: set their rights and links."))
        return redirect("accounts:staff-edit", pk=member.pk)


class StaffEditView(_StaffView, FormView):  # type: ignore[type-arg]
    template_name = "accounts/staff/edit.html"
    form_class = StaffForm

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "desk": self.desk, "target": self._target}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        desk = self.desk
        role_form = None
        if desk.zone.is_whole:
            role_form = StaffRoleForm(desk=desk, initial={"role": self._target.role})
        return super().get_context_data(target=self._target, role_form=role_form, **kwargs)

    def form_valid(self, form: StaffForm) -> HttpResponse:
        data = form.cleaned_data
        change = StaffChange(
            user_quota=data["user_quota"],
            key_quota=data["key_quota"],
            servers=data["servers"],
            plans=data["plans"],
            levels=form.levels(),
        )
        self.desk.update(self._target, change)
        messages.success(self.request, _("Saved."))
        return redirect("accounts:staff-edit", pk=self._target.pk)

    @cached_property
    def _target(self) -> User:
        return self.target()


class StaffRoleView(_StaffView):
    """An administrator changes the role of an owner or a moderator."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        desk = self.desk
        if not desk.zone.is_whole:
            raise Http404
        target = self.target()
        form = StaffRoleForm(request.POST, desk=desk)
        if form.is_valid():
            desk.set_role(target, form.cleaned_data["role"])
            messages.success(request, _("The role is changed."))
        return redirect("accounts:staff-edit", pk=target.pk)


class StaffBlockView(_StaffView):
    """Adds a member to a server, a plan or a user, or removes one, and goes back to its page."""

    remove = False

    def post(self, request: HttpRequest, kind: str, pk: int) -> HttpResponse:
        desk = self.desk
        obj = self._object(desk, kind, pk)
        if self.remove:
            member = get_object_or_404(desk.members(), pk=request.POST.get("member") or 0)
        else:
            form = StaffPickForm(request.POST, candidates=desk.members())
            if not form.is_valid():
                raise Http404
            member = form.cleaned_data["member"]
        try:
            self._apply(desk, obj, member)
        except NotAllowedError:
            messages.error(request, _("A link from the plan cannot be removed by hand."))
        return redirect(self._page(kind, pk))

    def _apply(self, desk: StaffDesk, obj: Server | Plan | User, member: User) -> None:
        if isinstance(obj, Server):
            (desk.remove_from_server if self.remove else desk.add_to_server)(obj, member)
        elif isinstance(obj, Plan):
            (desk.remove_from_plan if self.remove else desk.add_to_plan)(obj, member)
        elif self.remove:
            desk.unlink(obj, member)
        elif not desk.link(obj, member):
            messages.error(self.request, _("The staff member has as many users as allowed."))

    @staticmethod
    def _object(desk: StaffDesk, kind: str, pk: int) -> Server | Plan | User:
        sources: dict[str, QuerySet[Any]] = {
            "server": desk.zone.servers(),
            "plan": desk.zone.plans(),
            "user": desk.zone.users(),
        }
        if kind not in sources:
            raise Http404
        return get_object_or_404(sources[kind], pk=pk)  # type: ignore[no-any-return]

    @staticmethod
    def _page(kind: str, pk: int) -> str:
        names = {"server": "servers:detail", "plan": "plans:edit", "user": "dashboard:user"}
        return reverse(names[kind], args=[pk])
