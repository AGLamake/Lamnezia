"""Forms of the panel update: the version and the archive password, the restore after a rollback."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.backups.forms import PanelArchiveForm


class PanelUpdateForm(PanelArchiveForm):
    """The archive password typed twice, and the version the page offered."""

    version = forms.CharField(widget=forms.HiddenInput, max_length=20)


class RollbackRestoreForm(forms.Form):
    """The password of the archive made before the rolled back update."""

    restore_password = forms.CharField(
        label=_("Password of the archive"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )
