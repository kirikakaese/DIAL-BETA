"""Info pages: model + Markdown rendering (HTML escaping!), export/import round trip, event cloning."""
import datetime as dt

import pytest

from apps.events.export import export_event, import_event
from apps.pages.models import InfoPage
from apps.pages.rendering import first_line, render_markdown

pytestmark = pytest.mark.django_db


@pytest.fixture
def page(event, orga):
    return InfoPage.objects.create(event=event, slug="how-to-dect", title="DECT how-to",
                                   body="## Step 1\n\nDial **9002** for the site survey.", order=1,
                                   show_on_dashboard=True, updated_by=orga)


@pytest.fixture
def draft(event):
    return InfoPage.objects.create(event=event, slug="draft", title="Draft", body="wip", published=False, order=2)


# --------------------------------------------------------------------------- rendering

def test_markdown_renders_headings_lists_tables_links():
    html = str(render_markdown("## Hi\n\n- a\n- **b**\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n[DIAL](https://dial.example)"))
    assert '<h2 id="hi">Hi</h2>' in html
    assert "<li>a</li>" in html and "<strong>b</strong>" in html
    assert '<div class="table-wrap"><table>' in html and "<td>2</td>" in html
    assert '<a href="https://dial.example" rel="noopener" target="_blank">DIAL</a>' in html


def test_markdown_escapes_raw_html_and_javascript_links():
    src = '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n[x](javascript:alert(1))'
    html = str(render_markdown(src))
    assert "<script" not in html and "<img" not in html and 'onerror="' not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "javascript:" not in html and "<a>x</a>" in html


def test_markdown_code_blocks_keep_angle_brackets_readable():
    html = str(render_markdown("Use `<tag>` here.\n\n```\n<pre>&amp;\n```"))
    assert "<code>&lt;tag&gt;</code>" in html
    assert "&lt;pre&gt;&amp;amp;" in html  # single escaping inside code, still no raw tag
    assert "<pre>&" not in html


def test_markdown_result_is_safe_and_empty_body_ok():
    from django.utils.safestring import SafeString

    assert isinstance(render_markdown("x"), SafeString)
    assert str(render_markdown("")) == "" and str(render_markdown(None)) == ""


def test_first_line_strips_markdown_and_truncates():
    assert first_line("## Heading\n\ntext") == "Heading"
    assert first_line("\n\n- first *item* `x`\nmore") == "first item x"
    assert first_line("a" * 300).endswith("…") and len(first_line("a" * 300)) == 160
    assert first_line("") == ""


# --------------------------------------------------------------------------- model

def test_model_str_ordering_unique_and_properties(event, page, draft):
    assert str(page) == "DECT how-to (how-to-dect)"
    assert list(InfoPage.objects.filter(event=event).values_list("slug", flat=True)) == ["how-to-dect", "draft"]
    assert page.excerpt == "Step 1"
    assert "<strong>9002</strong>" in str(page.body_html)
    assert event.pages.count() == 2
    with pytest.raises(Exception):  # noqa: B017 - IntegrityError
        InfoPage.objects.create(event=event, slug="how-to-dect", title="dup")


def test_updated_by_set_null_on_user_delete(page, orga):
    orga.delete()
    page.refresh_from_db()
    assert page.updated_by is None


# --------------------------------------------------------------------------- export / import / clone

def test_export_import_round_trip(event, page, draft):
    data = export_event(event)
    assert [p["slug"] for p in data["pages"]] == ["how-to-dect", "draft"]
    assert data["pages"][0] == {"slug": "how-to-dect", "title": "DECT how-to", "order": 1, "published": True,
                                "show_on_dashboard": True, "body": "## Step 1\n\nDial **9002** for the site survey."}
    new = import_event(data, slug_override="demo-copy")
    copies = list(new.pages.all())
    assert [(p.slug, p.title, p.published, p.show_on_dashboard, p.order) for p in copies] == [
        ("how-to-dect", "DECT how-to", True, True, 1), ("draft", "Draft", False, False, 2)]
    assert copies[0].body == page.body and copies[0].updated_by is None
    assert InfoPage.objects.filter(event=event).count() == 2  # originals untouched


def test_import_without_pages_key_is_fine(event):
    data = export_event(event)
    data.pop("pages")
    assert import_event(data, slug_override="demo-copy").pages.count() == 0


def test_clone_copies_pages(event, page, draft, orga):
    today = dt.date.today()
    new = event.clone(name="Demo 2", slug="demo2", start_date=today, end_date=today, actor=orga)
    assert [(p.slug, p.published) for p in new.pages.all()] == [("how-to-dect", True), ("draft", False)]
    assert new.pages.get(slug="how-to-dect").body == page.body
    assert new.pages.get(slug="how-to-dect").updated_by is None
