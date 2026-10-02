"""Dependency-free accessibility linter for server-rendered HTML.

``check_html(html)`` parses a rendered page with :mod:`html.parser` and returns a list of findings
(``"<rule>: <element snippet> (line N)"``). It is deliberately small and pragmatic - it catches the
mistakes that are easy to make in Django templates (unlabelled inputs, icon-only buttons, images
without ``alt``, tables without headers, inline ``onclick`` handlers, heading jumps, ...). It is not
a replacement for axe/pa11y or manual screen-reader testing.

Used by ``apps/core/tests/test_a11y.py`` (renders the smoke-test URL list) and by
``manage.py pet_a11y``.

Rules
-----
img-alt                     every ``<img>`` has an ``alt`` attribute (empty is fine for decoration)
input-label                 form controls have a ``<label for>``, a wrapping ``<label>`` or an aria name
button-name                 ``<button>``/``<a href>`` have text, ``aria-label``, ``aria-labelledby`` or ``title``
link-text                   link text is not just "here"/"click here"/"more"/"link"
heading-order               exactly one ``<h1>``; no skipped levels (h2 -> h4)
landmarks                   exactly one ``<main>``; labelled ``<nav>`` when there are several;
                            ``<header>``/``<footer>`` on pages that have a ``<nav>``
table-headers               every ``<table>`` has a ``<th>`` (or ``role="presentation"``)
lang                        ``<html lang="...">``
no-onclick                  no inline ``onclick``/``onchange``/``onkey*``/... handlers
aria-hidden-focusable       ``aria-hidden="true"`` subtrees contain no focusable elements
duplicate-id                ``id`` values are unique
positive-tabindex           no ``tabindex`` > 0
iframe-title                ``<iframe>`` has a ``title``
role-on-div-needs-tabindex  ``role="button"``/``link``/... on a non-focusable element needs ``tabindex``
details-summary             every ``<details>`` has a ``<summary>``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
UNLABELLED_INPUT_TYPES = frozenset({"hidden", "submit", "button", "reset", "image"})
GENERIC_LINK_TEXT = frozenset({"here", "click here", "more", "link", "read more", "click"})
NATIVELY_FOCUSABLE = frozenset({"a", "button", "input", "select", "textarea", "summary", "iframe"})
WIDGET_ROLES = frozenset({"button", "link", "checkbox", "menuitem", "tab", "switch", "option", "radio"})
SNIPPET_ATTRS = ("id", "name", "type", "class", "href", "for", "role", "src")


@dataclass
class _El:
    tag: str
    attrs: dict[str, str | None]
    line: int
    text: list[str] = field(default_factory=list)
    has_th: bool = False
    has_summary: bool = False

    @property
    def aria_hidden(self) -> bool:
        return (self.attrs.get("aria-hidden") or "").strip().lower() == "true"


def _snippet(tag: str, attrs: dict[str, str | None]) -> str:
    parts = [f"<{tag}"]
    for k in SNIPPET_ATTRS:
        if k in attrs and attrs[k] is not None:
            v = attrs[k]
            if len(v) > 40:
                v = v[:37] + "..."
            parts.append(f' {k}="{v}"')
    return "".join(parts) + ">"


def _is_focusable(tag: str, attrs: dict[str, str | None]) -> bool:
    if attrs.get("tabindex", "").strip() == "-1":
        return False
    if "disabled" in attrs and tag in {"button", "input", "select", "textarea"}:
        return False
    if tag == "a":
        return "href" in attrs
    if tag == "input":
        return (attrs.get("type") or "text").lower() != "hidden"
    if tag in NATIVELY_FOCUSABLE:
        return True
    return "tabindex" in attrs or "contenteditable" in attrs


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.findings: list[str] = []
        self.stack: list[_El] = []
        self.is_document = False
        self.html_lang: str | None = None
        self.ids: dict[str, int] = {}
        self.label_for: set[str] = set()
        self.pending_inputs: list[tuple[_El, bool]] = []  # (element, wrapped in <label>)
        self.headings: list[tuple[int, int]] = []  # (level, line)
        self.mains = 0
        self.navs: list[_El] = []
        self.has_header = False
        self.has_footer = False

    # ------------------------------------------------------------------ helpers
    def _add(self, rule: str, el: _El | None = None, detail: str | None = None) -> None:
        if el is not None:
            msg = f"{rule}: {_snippet(el.tag, el.attrs)} (line {el.line})"
        else:
            msg = f"{rule}: {detail}"
        if detail and el is not None:
            msg += f" - {detail}"
        self.findings.append(msg)

    def _ancestor(self, *tags: str) -> _El | None:
        for el in reversed(self.stack):
            if el.tag in tags:
                return el
        return None

    def _inside_aria_hidden(self) -> bool:
        return any(el.aria_hidden for el in self.stack)

    def _feed_text(self, data: str) -> None:
        """Attach text to open elements for accessible-name computation (skipping aria-hidden parts)."""
        hidden = False
        for el in reversed(self.stack):
            if not hidden:
                el.text.append(data)
            if el.aria_hidden:
                hidden = True

    @staticmethod
    def _name(el: _El) -> str:
        for attr in ("aria-label", "aria-labelledby", "title"):
            if (el.attrs.get(attr) or "").strip():
                return el.attrs[attr].strip()
        return " ".join("".join(el.text).split())

    # ------------------------------------------------------------------ parser hooks
    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, push=False)

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, push=tag not in VOID)

    def _start(self, tag: str, raw_attrs, push: bool) -> None:
        attrs: dict[str, str | None] = {}
        for k, v in raw_attrs:
            attrs.setdefault(k.lower(), v)
        el = _El(tag, attrs, self.getpos()[0])

        for k in attrs:
            if k.startswith("on"):
                self._add("no-onclick", el, f"inline {k}= handler")
                break

        el_id = attrs.get("id")
        if el_id:
            self.ids[el_id] = self.ids.get(el_id, 0) + 1
            if self.ids[el_id] == 2:
                self._add("duplicate-id", el)

        ti = (attrs.get("tabindex") or "").strip()
        if ti.lstrip("-").isdigit() and int(ti) > 0:
            self._add("positive-tabindex", el)

        role = (attrs.get("role") or "").strip().lower()
        if role in WIDGET_ROLES and tag not in NATIVELY_FOCUSABLE and "tabindex" not in attrs:
            self._add("role-on-div-needs-tabindex", el)

        if self._inside_aria_hidden() and _is_focusable(tag, attrs):
            self._add("aria-hidden-focusable", el)

        if tag == "html":
            self.is_document = True
            self.html_lang = attrs.get("lang")
        elif tag == "img":
            if "alt" not in attrs:
                self._add("img-alt", el)
            elif attrs.get("alt"):
                self._feed_text(attrs["alt"])
        elif tag == "input":
            itype = (attrs.get("type") or "text").lower()
            if itype not in UNLABELLED_INPUT_TYPES:
                self.pending_inputs.append((el, self._ancestor("label") is not None))
            elif itype in {"submit", "button", "reset"} and attrs.get("value"):
                self._feed_text(attrs["value"])
            elif itype == "image" and "alt" not in attrs:
                self._add("img-alt", el)
        elif tag in {"select", "textarea"}:
            self.pending_inputs.append((el, self._ancestor("label") is not None))
        elif tag == "label" and attrs.get("for"):
            self.label_for.add(attrs["for"])
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.headings.append((int(tag[1]), el.line))
        elif tag == "main":
            self.mains += 1
        elif tag == "nav":
            self.navs.append(el)
        elif tag == "header":
            self.has_header = True
        elif tag == "footer":
            self.has_footer = True
        elif tag == "iframe" and not (attrs.get("title") or "").strip():
            self._add("iframe-title", el)
        elif tag == "th":
            table = self._ancestor("table")
            if table is not None:
                table.has_th = True
        elif tag == "summary":
            details = self._ancestor("details")
            if details is not None:
                details.has_summary = True

        # aria-label on a child contributes to the parent's accessible name (e.g. <a><span aria-label>)
        if attrs.get("aria-label") and tag not in {"a", "button"}:
            self._feed_text(attrs["aria-label"])

        if push:
            self.stack.append(el)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                closing = self.stack[i:]
                del self.stack[i:]
                for el in reversed(closing):
                    self._close(el)
                return
        # stray end tag - ignore

    def _close(self, el: _El) -> None:
        if el.tag in {"button", "a"}:
            if el.tag == "a" and "href" not in el.attrs:
                return
            name = self._name(el)
            if not name:
                self._add("button-name", el)
            elif el.tag == "a" and name.lower().strip(" .…!") in GENERIC_LINK_TEXT:
                self._add("link-text", el, f'text "{name}"')
        elif el.tag == "table":
            if not el.has_th and (el.attrs.get("role") or "").lower() != "presentation":
                self._add("table-headers", el)
        elif el.tag == "details" and not el.has_summary:
            self._add("details-summary", el)

    def handle_data(self, data):
        if self.stack and self.stack[-1].tag in {"script", "style"}:
            return
        if data.strip():
            self._feed_text(data)

    # ------------------------------------------------------------------ document-level checks
    def finish(self) -> list[str]:
        while self.stack:
            self._close(self.stack.pop())

        for el, wrapped in self.pending_inputs:
            a = el.attrs
            named = wrapped or any((a.get(k) or "").strip() for k in ("aria-label", "aria-labelledby", "title"))
            if not named and not (a.get("id") and a["id"] in self.label_for):
                self._add("input-label", el)

        if self.is_document:
            if not (self.html_lang or "").strip():
                self._add("lang", detail="<html> has no lang attribute")
            h1s = [line for level, line in self.headings if level == 1]
            if not h1s:
                self._add("heading-order", detail="page has no <h1>")
            elif len(h1s) > 1:
                self._add("heading-order", detail=f"{len(h1s)} <h1> elements (lines {', '.join(map(str, h1s))})")
            if self.mains != 1:
                self._add("landmarks", detail=f"expected exactly one <main>, found {self.mains}")
            if self.navs and not self.has_header:
                self._add("landmarks", detail="page has no <header>")
            if self.navs and not self.has_footer:
                self._add("landmarks", detail="page has no <footer>")
        if len(self.navs) > 1:
            for nav in self.navs:
                if not ((nav.attrs.get("aria-label") or nav.attrs.get("aria-labelledby") or "").strip()):
                    self._add("landmarks", nav, "several <nav> elements - each needs an aria-label")

        prev = None
        for level, line in self.headings:
            if prev is not None and level > prev + 1:
                self._add("heading-order", detail=f"<h{level}> follows <h{prev}> (line {line})")
            prev = level
        return self.findings


def check_html(html: str) -> list[str]:
    """Return accessibility findings for a rendered HTML document or fragment (empty list = clean)."""
    checker = _Checker()
    checker.feed(html)
    checker.close()
    return checker.finish()


# URL prefixes rendered by third-party UIs we do not own (Django admin, Swagger UI).
SKIP_PREFIXES = ("/admin/", "/api/docs/")


def smoke_page_lists(**kwargs) -> dict[str, list[str]]:
    """Load ``page_lists()`` from ``scripts/smoke.py`` (not a package) so tests and the linter share one URL list."""
    import importlib.util

    from django.conf import settings

    path = settings.BASE_DIR / "scripts" / "smoke.py"
    spec = importlib.util.spec_from_file_location("pet_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.page_lists(**kwargs)


def audit_url(client, url: str) -> list[str] | None:
    """GET ``url`` (following redirects); return findings for HTML 200 responses, ``None`` otherwise."""
    if url.startswith(SKIP_PREFIXES):
        return None
    response = client.get(url, follow=True)
    if response.status_code != 200 or not response.get("Content-Type", "").startswith("text/html"):
        return None
    return check_html(response.content.decode(response.charset or "utf-8"))
