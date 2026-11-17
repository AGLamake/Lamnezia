"""Forms of the panel update: the version and the password of the archive made before it."""

from django import forms

from apps.backups.forms import PanelArchiveForm


class PanelUpdateForm(PanelArchiveForm):
    """The archive password typed twice, and the version the page offered."""

    version = forms.CharField(widget=forms.HiddenInput, max_length=20)
