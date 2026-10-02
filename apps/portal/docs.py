"""Documentation pages: renders the Markdown files from ``docs/`` (and the changelog) inside the portal.

Design goals: no build step, no external services (Mermaid diagrams are enhanced client-side only when
the CDN is reachable, the source stays readable otherwise), results cached per file mtime so editing a
``.md`` shows up immediately in development while production pays the rendering cost once.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as etree

import markdown
from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from markdown.extensions import Extension
from markdown.extensions.toc import slugify
from markdown.treeprocessors import Treeprocessor

ROOT = Path(settings.BASE_DIR)


@dataclass(frozen=True)
class Doc:
    slug: str
    path: str  # relative to the project root
    label: str
    audience: str
    summary: str
    icon: str = "\U0001F4C4"


DOCS: list[Doc] = [
    Doc("user-guide", "docs/USER_GUIDE.md", _("User Guide"), _("Attendees"),
        _("Your account, picking a number, DECT handsets, softphones, call groups and services."), "\U0001F4F1"),
    Doc("event-guide", "docs/EVENT_GUIDE.md", _("Event Guide"), _("Admins & orga"),
        _("Creating an event, roles per event, number plan, lifecycle, export and cloning."), "\U0001F3AA"),
    Doc("operator-handbook", "docs/OPERATOR_HANDBOOK.md", _("Operator Handbook"), _("Orga / NOC"),
        _("Deployment, DECT and SIP setup, Asterisk operations, helpdesk, monitoring, emergencies."),
        "\U0001F6E0\uFE0F"),
    Doc("api", "docs/API.md", _("API & CLI"), _("Integrators"),
        _("REST API, authentication and scopes, webhooks and the pet command-line client."), "\U0001F50C"),
    Doc("architecture", "docs/ARCHITECTURE.md", _("Architecture"), _("Developers"),
        _("Components, data model, key flows, adapter pattern, security and design decisions."), "\U0001F3D7\uFE0F"),
    Doc("developing", "docs/DEVELOPING.md", _("Developing"), _("Contributors"),
        _("Repository layout, contracts between apps, testing and release workflow."), "\U0001F9EA"),
    Doc("changelog", "CHANGELOG.md", _("Changelog"), _("Everyone"),
        _("What changed between releases."), "\U0001F4DC"),
]
_BY_SLUG = {d.slug: d for d in DOCS}
_BY_FILE = {Path(d.path).name.lower(): d.slug for d in DOCS}


@dataclass
class Rendered:
    mtime: float
    title: str
    body: str
    toc: list[dict] = field(default_factory=list)
    has_mermaid: bool = False
    sections: list[tuple[str, str]] = field(default_factory=list)  # (anchor, heading) for h2


_cache: dict[str, Rendered] = {}


# --------------------------------------------------------------------------- markdown plumbing

class _LinkRewriter(Treeprocessor):
    """Point ``*.md`` links at the portal pages and ``openapi.yaml`` at the live schema."""

    def run(self, root):
        for a in root.iter("a"):
            href = a.get("href") or ""
            if href.startswith(("http://", "https://", "mailto:", "#", "/")):
                continue
            path, _, frag = href.partition("#")
            name = Path(path).name.lower()
            if name.endswith(".md") and name in _BY_FILE:
                a.set("href", reverse("portal:docs_page", args=[_BY_FILE[name]]) + (f"#{frag}" if frag else ""))
            elif name == "openapi.yaml":
                a.set("href", reverse("schema"))
        for el in root.iter("a"):
            if (el.get("href") or "").startswith(("http://", "https://")):
                el.set("rel", "noopener")
                el.set("target", "_blank")


class _TaskLists(Treeprocessor):
    """``- [ ] item`` / ``- [x] item`` -> read-only checkboxes (tight and loose lists)."""

    def run(self, root):
        for li in root.iter("li"):
            holder = li
            if not (li.text or "").strip() and len(li) and li[0].tag == "p":
                holder = li[0]
            text = holder.text or ""
            m = re.match(r"^\[( |x|X)\]\s+", text)
            if not m:
                continue
            box = etree.Element("span", {"class": "task-box" + (" done" if m.group(1) != " " else ""),
                                         "aria-hidden": "true"})
            box.tail = text[m.end():]
            holder.text = ""
            holder.insert(0, box)
            li.set("class", (li.get("class", "") + " task").strip())


class _PetDocsExtension(Extension):
    def extendMarkdown(self, md):  # noqa: N802 - markdown API
        md.treeprocessors.register(_LinkRewriter(md), "pet_links", 5)
        md.treeprocessors.register(_TaskLists(md), "pet_tasks", 4)


_MERMAID = re.compile(r"^```mermaid\n(.*?)^```\s*$", re.S | re.M)


def _extract_mermaid(text: str) -> tuple[str, bool]:
    found = False

    def repl(m):
        nonlocal found
        found = True
        return f'\n<pre class="mermaid">{html.escape(m.group(1).strip())}</pre>\n'

    return _MERMAID.sub(repl, text), found


def _make_md() -> markdown.Markdown:
    return markdown.Markdown(
        extensions=["tables", "fenced_code", "codehilite", "toc", "attr_list", "sane_lists",
                    "md_in_html", _PetDocsExtension()],
        extension_configs={
            "codehilite": {"css_class": "highlight", "guess_lang": False, "noclasses": False},
            "toc": {"toc_depth": "2-3", "permalink": "#", "permalink_class": "headerlink",
                    "permalink_title": str(_("Link to this section"))},
        },
        output_format="html5",
    )


def _title_and_body(text: str) -> tuple[str, str]:
    m = re.match(r"^#\s+(.+?)\s*\n", text)
    if m:
        return m.group(1).strip(), text[m.end():]
    return "", text


def _wrap_tables(body: str) -> str:
    return body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")


def get(slug: str) -> Rendered:
    doc = _BY_SLUG.get(slug)
    if doc is None:
        raise Http404
    path = ROOT / doc.path
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError as exc:
        raise Http404 from exc
    cached = _cache.get(slug)
    if cached and cached.mtime == mtime:
        return cached
    text = path.read_text(encoding="utf-8")
    title, body_md = _title_and_body(text)
    body_md, has_mermaid = _extract_mermaid(body_md)
    md = _make_md()
    body = _wrap_tables(md.convert(body_md))
    toc = md.toc_tokens
    sections = [(t["id"], t["name"]) for t in toc]
    rendered = Rendered(mtime, title or doc.slug.replace("-", " ").title(), body, toc, has_mermaid, sections)
    _cache[slug] = rendered
    return rendered


# --------------------------------------------------------------------------- search

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_MAX_RESULTS = 40


def search(query: str) -> list[dict]:
    """Case-insensitive substring search over every doc, grouped by nearest heading."""
    q = query.strip().lower()
    if len(q) < 2:
        return []
    results: list[dict] = []
    for doc in DOCS:
        path = ROOT / doc.path
        if not path.exists():
            continue
        title = ""
        heading, anchor = "", ""
        seen: set[str] = set()
        in_fence = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                m = _HEADING.match(line)
                if m:
                    if len(m.group(1)) == 1 and not title:
                        title = m.group(2)
                        continue
                    heading = m.group(2)
                    anchor = slugify(re.sub(r"[`*_]", "", heading), "-")
                    continue
            if q in line.lower() and anchor not in seen:
                seen.add(anchor)
                results.append({"doc": doc, "title": title, "heading": heading, "anchor": anchor,
                                "snippet": _snippet(line, q)})
                if len(results) >= _MAX_RESULTS:
                    return results
    return results


def _snippet(line: str, q: str) -> str:
    plain = re.sub(r"[`*_|#>\[\]]", " ", line).strip()
    plain = re.sub(r"\s{2,}", " ", plain)
    i = plain.lower().find(q)
    if i < 0:
        return html.escape(plain[:160])
    start = max(0, i - 70)
    end = min(len(plain), i + len(q) + 90)
    piece = plain[start:end]
    j = piece.lower().find(q)
    out = (html.escape(piece[:j]) + "<mark>" + html.escape(piece[j:j + len(q)]) + "</mark>"
           + html.escape(piece[j + len(q):]))
    return ("…" if start else "") + out + ("…" if end < len(plain) else "")


# --------------------------------------------------------------------------- views

def index(request):
    q = (request.GET.get("q") or "").strip()
    cards = []
    for doc in DOCS:
        try:
            r = get(doc.slug)
        except Http404:
            continue
        cards.append({"doc": doc, "title": r.title, "sections": r.sections[:6], "more": max(0, len(r.sections) - 6)})
    return render(request, "portal/docs/index.html", {
        "cards": cards, "q": q, "results": search(q) if q else None,
    })


def page(request, slug):
    r = get(slug)
    doc = _BY_SLUG[slug]
    return render(request, "portal/docs/page.html", {
        "doc": doc, "docs": DOCS, "title": r.title, "body": r.body, "toc": r.toc,
        "has_mermaid": r.has_mermaid, "q": (request.GET.get("q") or "").strip(),
    })
