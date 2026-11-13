"""Reply templates of the inbox: administrators add, edit and delete them (DESIGN.md 6.7)."""

from django.contrib import messages
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.accounts.permissions import AdminRequiredMixin
from apps.audit.services import journal as audit
from apps.support.forms import ReplyTemplateForm
from apps.support.models import ReplyTemplate

_LIST = reverse_lazy("support:templates")


class TemplateListView(AdminRequiredMixin, ListView):  # type: ignore[type-arg]
    model = ReplyTemplate
    template_name = "support/inbox/templates.html"
    context_object_name = "templates"


class TemplateCreateView(AdminRequiredMixin, CreateView):  # type: ignore[type-arg]
    model = ReplyTemplate
    form_class = ReplyTemplateForm
    template_name = "support/inbox/template_form.html"
    success_url = _LIST

    def form_valid(self, form: "ModelForm[ReplyTemplate]") -> HttpResponse:
        response = super().form_valid(form)
        audit.log_request(self.request, "support.template_create", form.instance)
        return response


class TemplateUpdateView(AdminRequiredMixin, UpdateView):  # type: ignore[type-arg]
    model = ReplyTemplate
    form_class = ReplyTemplateForm
    template_name = "support/inbox/template_form.html"
    context_object_name = "template"
    success_url = _LIST

    def form_valid(self, form: "ModelForm[ReplyTemplate]") -> HttpResponse:
        response = super().form_valid(form)
        audit.log_request(self.request, "support.template_update", form.instance)
        return response


class TemplateDeleteView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        template = get_object_or_404(ReplyTemplate, pk=pk)
        audit.log_request(request, "support.template_delete", template)
        template.delete()
        messages.success(request, _("The template is deleted."))
        return redirect(_LIST)
