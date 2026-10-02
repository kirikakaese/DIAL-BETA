"""In-app documentation: Markdown from ``docs/`` rendered at /docs/ with TOC, search and link rewriting."""
import pytest
from django.urls import reverse

from apps.portal import docs

pytestmark = pytest.mark.django_db


def test_index_lists_every_guide(client):
    r = client.get(reverse("portal:docs_index"))
    assert r.status_code == 200
    for d in docs.DOCS:
        assert reverse("portal:docs_page", args=[d.slug]).encode() in r.content
    assert b"docs-search" in r.content


@pytest.mark.parametrize("slug", [d.slug for d in docs.DOCS])
def test_every_guide_renders(client, slug):
    r = client.get(reverse("portal:docs_page", args=[slug]))
    assert r.status_code == 200
    body = r.content.decode()
    assert 'class="doc-body"' in body and "<h2 id=" in body
    assert 'class="headerlink"' in body  # anchors on headings
    assert "<table>" not in body.replace('<div class="table-wrap"><table>', "")  # every table is wrapped


def test_unknown_guide_404(client):
    assert client.get(reverse("portal:docs_page", args=["nope"])).status_code == 404


def test_event_guide_features(client):
    r = docs.get("event-guide")
    assert r.title.startswith("PET Event Guide")
    assert 'href="/docs/operator-handbook/"' in r.body  # OPERATOR_HANDBOOK.md -> portal page
    assert ".md" not in "".join(h for h in r.body.split('href="')[1:] if h.startswith("/docs"))
    assert '<pre class="mermaid">stateDiagram-v2' in r.body and r.has_mermaid
    assert '<li class="task"><span aria-hidden="true" class="task-box"></span>' in r.body
    assert [t["name"] for t in r.toc][:2] == ["1. Who may do what",
                                               "2. Before you create an event (server checklist)"]
    page = client.get(reverse("portal:docs_page", args=["event-guide"])).content.decode()
    assert "mermaid.esm.min.mjs" in page and 'href="#1-who-may-do-what"' in page


def test_api_doc_links_to_live_schema():
    assert 'href="/api/schema/"' in docs.get("api").body


def test_external_links_open_in_new_tab():
    body = docs.get("changelog").body
    assert 'href="https://keepachangelog.com/en/1.1.0/" rel="noopener" target="_blank"' in body


def test_cache_invalidates_on_mtime(tmp_path, monkeypatch):
    doc = docs.Doc("tmp", "TMP.md", "Tmp", "x", "y")
    monkeypatch.setitem(docs._BY_SLUG, "tmp", doc)
    monkeypatch.setattr(docs, "ROOT", tmp_path)
    f = tmp_path / "TMP.md"
    f.write_text("# Title one\n\n## A\n\ntext\n")
    first = docs.get("tmp")
    assert first.title == "Title one" and first.sections == [("a", "A")]
    assert docs.get("tmp") is first
    f.write_text("# Title two\n\n## B\n\ntext\n")
    import os
    os.utime(f, (first.mtime + 5, first.mtime + 5))
    second = docs.get("tmp")
    assert second is not first and second.title == "Title two"
    docs._cache.pop("tmp", None)


def test_search_groups_by_heading_and_highlights(client):
    results = docs.search("dial-to-claim")
    assert results and all("dial-to-claim" in r["snippet"].lower() for r in results)
    hit = next(r for r in results if r["doc"].slug == "user-guide")
    assert hit["anchor"] and hit["heading"]
    assert "<mark>" in hit["snippet"]
    r = client.get(reverse("portal:docs_index"), {"q": "dial-to-claim"})
    assert r.status_code == 200 and b"docs-result-crumb" in r.content
    assert f"/docs/user-guide/#{hit['anchor']}".encode() in r.content


def test_search_too_short_or_empty(client):
    assert docs.search("a") == [] and docs.search("  ") == []
    r = client.get(reverse("portal:docs_index"), {"q": "zzzzqqqq-not-there"})
    assert r.status_code == 200 and b"Nothing found" in r.content


def test_search_escapes_html():
    results = docs.search("<script>")
    assert all("<script>" not in r["snippet"] for r in results)


def test_docs_link_in_navigation(client):
    r = client.get(reverse("portal:home"))
    assert reverse("portal:docs_index").encode() in r.content
