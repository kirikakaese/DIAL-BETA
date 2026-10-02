"""Unit tests for every rule in ``apps.core.a11y.check_html`` using tiny HTML snippets."""

from __future__ import annotations

import pytest

from apps.core.a11y import check_html

PAGE = ('<!DOCTYPE html><html lang="en"><head><title>t</title></head><body>'
        '<header><nav aria-label="Main"><a href="/">Home</a></nav></header>'
        "<main><h1>Title</h1>{body}</main><footer>f</footer></body></html>")


def page(body: str = "") -> str:
    return PAGE.format(body=body)


def rules(html: str) -> set[str]:
    return {f.split(":", 1)[0] for f in check_html(html)}


def test_clean_page_has_no_findings():
    assert check_html(page("<p>hello</p>")) == []


# --------------------------------------------------------------------------- img-alt
def test_img_alt():
    assert rules('<img src="x.png">') == {"img-alt"}
    assert rules('<img src="x.png" alt="">') == set()
    assert rules('<img src="x.png" alt="A photo">') == set()
    assert rules('<input type="image" src="x.png">') == {"img-alt"}


# --------------------------------------------------------------------------- input-label
@pytest.mark.parametrize("html", [
    '<input type="text" name="q">',
    '<select name="s"><option>a</option></select>',
    "<textarea name=t></textarea>",
    '<label for="other">x</label><input id="q" type="text">',
])
def test_input_without_label(html):
    assert rules(html) == {"input-label"}


@pytest.mark.parametrize("html", [
    '<label for="q">Search</label><input id="q" type="search">',
    '<input id="q" type="search"><label for="q">label after</label>',
    '<label>Search <input type="search"></label>',
    '<input type="text" aria-label="Search">',
    '<input type="text" aria-labelledby="h">',
    '<input type="text" title="Search">',
    '<input type="hidden" name="csrf">',
    '<input type="submit" value="Go">',
    '<label>Pick <select><option>a</option></select></label>',
])
def test_labelled_inputs_are_fine(html):
    assert rules(html) == set()


# --------------------------------------------------------------------------- button-name / link-text
@pytest.mark.parametrize("html", [
    "<button></button>",
    '<button><span aria-hidden="true">☰</span></button>',
    '<a href="/x"></a>',
    '<a href="/x"><img src="i.png" alt=""></a>',
])
def test_button_without_name(html):
    assert rules(html) == {"button-name"}


@pytest.mark.parametrize("html", [
    "<button>Save</button>",
    '<button aria-label="Close"></button>',
    '<button title="Close">×</button>',
    '<button><span aria-hidden="true">☰</span><span class="sr-only">Menu</span></button>',
    '<a href="/x"><img src="i.png" alt="Logo"></a>',
    '<a href="/x"><span aria-label="Home"></span></a>',
    "<a>not a link (no href)</a><a></a>",
    '<a href="/x"><span class="badge">3</span></a>',
])
def test_named_buttons_are_fine(html):
    assert rules(html) == set()


@pytest.mark.parametrize("text", ["here", "Click here", "more", "Read more…", "link"])
def test_generic_link_text(text):
    assert rules(f'<a href="/x">{text}</a>') == {"link-text"}


# --------------------------------------------------------------------------- heading-order
def test_heading_order_document_level():
    no_h1 = page().replace("<h1>Title</h1>", "")
    assert rules(no_h1) == {"heading-order"}
    assert rules(page("<h1>Second</h1>")) == {"heading-order"}
    assert rules(page("<h2>a</h2><h3>b</h3><h2>c</h2>")) == set()
    assert rules(page("<h2>a</h2><h4>skipped</h4>")) == {"heading-order"}
    assert rules(page("<h3>skipped</h3>")) == {"heading-order"}


def test_heading_order_allows_sr_only_h1_and_sidebar_h2_before_h1():
    html = page("").replace("<h1>Title</h1>", '<h1 class="sr-only">Hidden title</h1>')
    assert rules(html) == set()
    html = page("").replace("<main>", '<aside><nav aria-label="Side"><h2>Section</h2></nav></aside><main>')
    assert rules(html) == set()


def test_fragments_skip_document_level_rules():
    assert rules("<h2>a</h2><p>b</p>") == set()
    assert rules("<h2>a</h2><h4>b</h4>") == {"heading-order"}


# --------------------------------------------------------------------------- landmarks
def test_landmarks():
    assert rules(page().replace("<main>", "<div>").replace("</main>", "</div>")) == {"landmarks"}
    assert rules(page("<main>second</main>")) == {"landmarks"}
    assert rules(page().replace("<header>", "").replace("</header>", "")) == {"landmarks"}
    assert rules(page().replace("<footer>f</footer>", "")) == {"landmarks"}
    two_navs = page('<nav><a href="/a">a</a></nav>')
    assert rules(two_navs) == {"landmarks"}
    assert rules(page('<nav aria-label="Sub"><a href="/a">a</a></nav>')) == set()


def test_standalone_page_without_nav_needs_only_main():
    html = '<html lang="en"><body><main><h1>Print</h1><p>sheet</p></main></body></html>'
    assert check_html(html) == []


# --------------------------------------------------------------------------- table-headers
def test_table_headers():
    assert rules("<table><tr><td>1</td></tr></table>") == {"table-headers"}
    assert rules("<table><thead><tr><th>n</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>") == set()
    assert rules('<table role="presentation"><tr><td>1</td></tr></table>') == set()
    assert rules("<table><tr><th scope=row>n</th><td>1</td></tr></table>") == set()


# --------------------------------------------------------------------------- lang
def test_lang():
    assert rules(page().replace('<html lang="en">', "<html>")) == {"lang"}
    assert rules(page().replace('<html lang="en">', '<html lang="">')) == {"lang"}


# --------------------------------------------------------------------------- no-onclick
@pytest.mark.parametrize("attr", ["onclick", "onchange", "onkeydown", "onkeyup", "onsubmit", "oninput"])
def test_no_inline_handlers(attr):
    assert rules(f'<button {attr}="x()">Go</button>') == {"no-onclick"}


def test_data_action_is_fine():
    assert rules('<button type="button" data-action="print">Print</button>') == set()


# --------------------------------------------------------------------------- aria-hidden-focusable
def test_aria_hidden_focusable():
    assert rules('<div aria-hidden="true"><button>x</button></div>') == {"aria-hidden-focusable"}
    assert rules('<div aria-hidden="true"><a href="/x">x</a></div>') == {"aria-hidden-focusable"}
    assert rules('<div aria-hidden="true"><label>w <input type="text"></label></div>') == {"aria-hidden-focusable"}
    assert rules('<div aria-hidden="true"><input type="text" tabindex="-1" aria-label="hp"></div>') == set()
    assert rules('<div aria-hidden="true"><span>☰</span><a>anchor</a></div>') == set()
    assert rules('<span aria-hidden="true">icon</span><button>x</button>') == set()


# --------------------------------------------------------------------------- duplicate-id
def test_duplicate_id():
    assert rules('<p id="a">1</p><p id="a">2</p>') == {"duplicate-id"}
    assert rules('<p id="a">1</p><p id="b">2</p>') == set()


# --------------------------------------------------------------------------- positive-tabindex
def test_positive_tabindex():
    assert rules('<div tabindex="1">x</div>') == {"positive-tabindex"}
    assert rules('<div tabindex="0">x</div><div tabindex="-1">y</div>') == set()


# --------------------------------------------------------------------------- iframe-title
def test_iframe_title():
    assert rules('<iframe src="/x"></iframe>') == {"iframe-title"}
    assert rules('<iframe src="/x" title="Map"></iframe>') == set()


# --------------------------------------------------------------------------- role-on-div-needs-tabindex
def test_role_widget_needs_tabindex():
    assert rules('<div role="button">x</div>') == {"role-on-div-needs-tabindex"}
    assert rules('<div role="button" tabindex="0">x</div>') == set()
    assert rules('<button role="button">x</button>') == set()
    assert rules('<div role="status">x</div>') == set()


# --------------------------------------------------------------------------- details-summary
def test_details_summary():
    assert rules("<details><p>x</p></details>") == {"details-summary"}
    assert rules("<details><summary>More</summary><p>x</p></details>") == set()
    assert rules("<details><summary>a</summary><details><p>x</p></details></details>") == {"details-summary"}


# --------------------------------------------------------------------------- output format
def test_finding_format_contains_rule_snippet_and_line():
    (finding,) = check_html('<p>\n<img id="logo" src="l.png">')
    assert finding.startswith("img-alt: <img")
    assert 'id="logo"' in finding and "(line 2)" in finding
