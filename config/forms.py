"""Project-wide form rendering: every `{{ form }}` uses the shared templates and CSS classes."""

from typing import Any

from django.forms import BoundField, CheckboxInput, CheckboxSelectMultiple, RadioSelect, Widget
from django.forms.renderers import TemplatesSetting

# Widgets that render a list of inputs and are styled by their option template instead.
_CHOICE_LISTS = (CheckboxSelectMultiple, RadioSelect)


class StyledBoundField(BoundField):
    """Adds the `input` / `checkbox` classes and the error state to the widget."""

    def build_widget_attrs(
        self, attrs: dict[str, Any], widget: Widget | None = None
    ) -> dict[str, Any]:
        attrs = super().build_widget_attrs(attrs, widget)
        widget = widget or self.field.widget
        if isinstance(widget, _CHOICE_LISTS):
            return attrs
        classes = ["checkbox" if isinstance(widget, CheckboxInput) else "input"]
        if self.errors:
            classes.append("input-invalid")
        # Explicit attrs win over widget.attrs when Django merges them, so keep the widget's class.
        own = str(attrs.get("class") or widget.attrs.get("class", ""))
        attrs["class"] = " ".join([*classes, own]).strip()
        return attrs


class FormRenderer(TemplatesSetting):
    form_template_name = "forms/div.html"
    field_template_name = "forms/field.html"
    bound_field_class = StyledBoundField
