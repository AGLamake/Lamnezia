from django import forms
from django.template import Context, Template

from apps.core.templatetags.ui import active_filters


def render(source: str, **context: object) -> str:
    return Template("{% load ui %}" + source).render(Context(context))


def test_icon_is_hidden_from_screen_readers_by_default() -> None:
    html = render('{% icon "close" %}')

    assert html.startswith('<svg class="icon"')
    assert 'aria-hidden="true"' in html


def test_icon_with_a_label_is_announced() -> None:
    html = render('{% icon "trash" "icon-sm" label="Delete" %}')

    assert 'role="img"' in html
    assert 'aria-label="Delete"' in html
    assert 'class="icon-sm"' in html


def test_unknown_icon_renders_nothing() -> None:
    assert render('{% icon "nope" %}') == ""


def test_sparkline_tag_describes_the_chart() -> None:
    html = render('{% sparkline values "CPU by day" floor=0 ceiling=100 %}', values=[10, None, 30])

    assert 'role="img"' in html
    assert 'aria-label="CPU by day"' in html
    assert html.count("<polyline") == 3  # two runs and the end dot


def test_bar_chart_tag_draws_bars_and_a_table_for_screen_readers() -> None:
    html = render(
        '{% bar_chart labels series names "Traffic" %}',
        labels=["Mon", "Tue"],
        series=[[1024, 0], [2048, 512]],
        names=["Downloaded", "Uploaded"],
    )

    assert html.count('class="bar-chart-1"') == 1
    assert html.count('class="bar-chart-2"') == 2
    assert "<caption>Traffic</caption>" in html
    assert html.count('class="bar-chart-tip bar-chart-tip-') == 2
    assert '<p class="font-semibold">Mon</p>' in html
    assert "3.0\xa0KB" in html  # the total of Mon in its tooltip


class _Filters(forms.Form):
    status = forms.CharField(required=False)
    search = forms.CharField(required=False)
    page = forms.CharField(required=False, widget=forms.HiddenInput)


def test_active_filters_count_the_filled_visible_fields() -> None:
    assert active_filters(_Filters()) == 0
    assert active_filters(_Filters({"status": "", "page": "2"})) == 0
    assert active_filters(_Filters({"status": "on", "search": "ab", "page": "2"})) == 2
