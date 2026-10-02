"""Markdown rendering for orga-authored content.

Unlike the in-app docs (``apps.portal.docs``), page bodies are written by event organisers, so the
text is HTML-escaped *before* Markdown runs: raw tags never reach the output, only Markdown syntax
does. A tree processor additionally drops ``javascript:``-style link targets, and no extension that
allows arbitrary attributes (``attr_list``, ``md_in_html``) is enabled.
"""
from __future__ import annotations

import re

import markdown
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

_SAFE_URL = re.compile(r"^(https?://|mailto:|tel:|sip:|/|#|\.{0,2}/|[\w.-]+(/|$))", re.I)


class _SafeLinks(Treeprocessor):
    """Strip ``href``/``src`` values with unexpected schemes; open external links in a new tab."""

    def run(self, root):
        for el in root.iter():
            for attr in ("href", "src"):
                value = (el.get(attr) or "").strip()
                if value and not _SAFE_URL.match(value):
                    del el.attrib[attr]
            href = el.get("href") or ""
            if el.tag == "a" and href.lower().startswith(("http://", "https://")):
                el.set("rel", "noopener")
                el.set("target", "_blank")


class _PagesExtension(Extension):
    def extendMarkdown(self, md):  # noqa: N802 - markdown API
        md.treeprocessors.register(_SafeLinks(md), "dial_pages_links", 5)


def _make_md() -> markdown.Markdown:
    return markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "toc", _PagesExtension()],
        extension_configs={"toc": {"toc_depth": "2-4"}},
        output_format="html5",
    )


def _wrap_tables(body: str) -> str:
    return body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")


_CODE = re.compile(r"(<code[^>]*>)(.*?)(</code>)", re.S)
_DOUBLE_ESCAPED = re.compile(r"&amp;(lt|gt|amp|quot|#x27|#39);")


def _fix_code_entities(body: str) -> str:
    """Markdown escapes code spans itself, so pre-escaped ``&lt;`` ends up as ``&amp;lt;`` there. Undo that one
    level inside ``<code>`` only - the result is still an entity, never a tag."""
    return _CODE.sub(lambda m: m.group(1) + _DOUBLE_ESCAPED.sub(r"&\1;", m.group(2)) + m.group(3), body)


def render_markdown(text: str) -> SafeString:
    """Escape ``text`` (so no raw HTML survives), convert the Markdown and mark the result safe."""
    html = _make_md().convert(escape(text or ""))
    return mark_safe(_fix_code_entities(_wrap_tables(html)))  # noqa: S308 - input was escaped before rendering


_MD_NOISE = re.compile(r"^[#>*\-+\s]+|[*_`]+")


def first_line(text: str, limit: int = 160) -> str:
    """Plain-text teaser: the first non-empty line without Markdown decoration."""
    for line in (text or "").splitlines():
        plain = _MD_NOISE.sub("", line.strip()).strip()
        if plain:
            return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"
    return ""
