"""Forms of the backups app: the password of a new panel archive, the archive to restore."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.backups.services.archive_cipher import MIN_PASSWORD_LENGTH


class ArchivePasswordField(forms.CharField):
    """A new password, kept as typed: spaces at the ends are part of it."""

    def __init__(self, **kwargs: Any) -> None:
        widget = forms.PasswordInput(attrs={"autocomplete": "new-password"})
        super().__init__(strip=False, min_length=MIN_PASSWORD_LENGTH, widget=widget, **kwargs)


class PanelArchiveForm(forms.Form):
    """A password typed twice; the archive cannot be opened without it."""

    password = ArchivePasswordField(label=_("Archive password"))
    confirmation = ArchivePasswordField(label=_("Password again"))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["password"].help_text = _(
            "At least %(count)d characters. The panel does not keep it: without the password "
            "the archive cannot be restored."
        ) % {"count": MIN_PASSWORD_LENGTH}

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if data.get("password") != data.get("confirmation"):
            self.add_error("confirmation", _("The passwords differ."))
        return data


class PanelRestoreForm(forms.Form):
    """An archive to restore: uploaded now or one kept on the panel, and its password."""

    upload = forms.FileField(label=_("Archive file"), required=False)
    archive = forms.ChoiceField(label=_("Or an archive on the panel"), required=False)
    restore_password = forms.CharField(
        label=_("Password of the archive"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )

    def __init__(self, *args: Any, archives: list[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        archive = self.fields["archive"]
        assert isinstance(archive, forms.ChoiceField)
        archive.choices = [("", "—"), *((name, name) for name in archives)]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if bool(data.get("upload")) == bool(data.get("archive")):
            raise forms.ValidationError(
                _("Choose either a file to upload or an archive on the panel.")
            )
        return data
