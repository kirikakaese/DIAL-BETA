"""Info pages portal: list/show visibility, orga management flow with audit + webhook, dashboard tag."""
from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse

from apps.core.models import AuditLog
from apps.pages.models import InfoPage

pytestmark = pytest.mark.django_db


@pytest.fixture
def page(event, orga):
    return InfoPage.objects.create(event=event, slug="how-to-dect", title="DECT how-to",
                                   body="## Step 1\n\nDial **9002**.\n\n<script>alert(1)</script>", order=1,
                                   show_on_dashboard=True, updated_by=orga)


@pytest.fixture
def draft(event):
    return InfoPage.objects.create(event=event, slug="draft", title="Secret draft", body="wip", published=False)


def _client(user=None):
    c = Client()
    if user is not None:
        c.force_login(user)
    return c


def _url(name, *args):
    return reverse(f"pages:{name}", args=["demo", *args])


# --------------------------------------------------------------------------- urls

def test_urls_are_mounted_under_event():
    assert _url("index") == "/e/demo/pages/"
    assert _url("show", "x") == "/e/demo/pages/x/"
    assert _url("manage") == "/e/demo/pages/manage/"
    assert _url("create") == "/e/demo/pages/manage/new/"
    assert _url("edit", "x") == "/e/demo/pages/manage/x/"
    assert _url("delete", "x") == "/e/demo/pages/manage/x/delete/"


# --------------------------------------------------------------------------- reading

def test_member_sees_published_pages_only(member, user, page, draft):
    c = _client(user)
    r = c.get(_url("index"))
    assert r.status_code == 200
    body = r.content.decode()
    assert "DECT how-to" in body and "Secret draft" not in body and "Manage pages" not in body
    assert c.get(_url("show", "how-to-dect")).status_code == 200
    assert c.get(_url("show", "draft")).status_code == 404
    assert c.get(_url("show", "nope")).status_code == 404


def test_show_renders_markdown_escaped(member, user, page):
    r = _client(user).get(_url("show", "how-to-dect"))
    body = r.content.decode()
    assert '<h2 id="step-1">Step 1</h2>' in body and "<strong>9002</strong>" in body
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert 'class="doc-body"' in body


def test_orga_sees_unpublished_with_badge(orga, page, draft):
    c = _client(orga)
    body = c.get(_url("index")).content.decode()
    assert "Secret draft" in body and "unpublished" in body and "Manage pages" in body
    r = c.get(_url("show", "draft"))
    assert r.status_code == 200 and "not published" in r.content.decode()


def test_anonymous_sees_public_event_pages(event, page, draft):
    c = _client()
    r = c.get(_url("index"))
    assert r.status_code == 200 and "DECT how-to" in r.content.decode()
    assert c.get(_url("show", "draft")).status_code == 404


def test_unknown_event_404(user):
    assert _client(user).get(reverse("pages:index", args=["nope"])).status_code == 404


# --------------------------------------------------------------------------- permissions

@pytest.mark.parametrize("name,args", [("manage", ()), ("create", ()), ("edit", ("how-to-dect",))])
def test_member_gets_403_on_manage_pages(member, user, page, name, args):
    assert _client(user).get(_url(name, *args)).status_code == 403


def test_member_cannot_delete(member, user, page):
    assert _client(user).post(_url("delete", "how-to-dect")).status_code == 403
    assert InfoPage.objects.filter(pk=page.pk).exists()


def test_anonymous_gets_403_on_manage(event):
    assert _client().get(_url("manage")).status_code == 403


def test_delete_requires_post(orga, page):
    assert _client(orga).get(_url("delete", "how-to-dect")).status_code == 405


# --------------------------------------------------------------------------- orga flow

def test_manage_table_lists_everything(orga, page, draft):
    body = _client(orga).get(_url("manage")).content.decode()
    assert "DECT how-to" in body and "Secret draft" in body
    assert "how-to-dect" in body and "draft" in body and "data-confirm" in body
    assert _url("create") in body and _url("edit", "how-to-dect") in body and _url("delete", "draft") in body


def test_create_edit_delete_flow_with_audit_and_webhook(orga, event):
    c = _client(orga)
    assert c.get(_url("create")).status_code == 200
    with mock.patch("apps.pages.views.emit") as emit:
        r = c.post(_url("create"), {"title": "House rules", "slug": "rules", "body": "Be *nice*.", "order": 3,
                                    "published": "on", "show_on_dashboard": "on"})
    assert r.status_code == 302 and r["Location"] == _url("manage")
    page = InfoPage.objects.get(event=event, slug="rules")
    assert page.title == "House rules" and page.order == 3 and page.published and page.show_on_dashboard
    assert page.updated_by == orga
    log = AuditLog.objects.filter(action="create", target_id=str(page.pk)).get()
    assert log.actor == orga and log.event == event and "House rules" in log.message
    emit.assert_called_once()
    assert emit.call_args.args[0] == "page.updated"
    assert emit.call_args.args[1]["slug"] == "rules" and emit.call_args.args[1]["action"] == "create"
    assert emit.call_args.kwargs["event"] == event

    # edit
    assert c.get(_url("edit", "rules")).status_code == 200
    with mock.patch("apps.pages.views.emit") as emit:
        r = c.post(_url("edit", "rules"), {"title": "House rules v2", "slug": "rules", "body": "Be *nice*!",
                                          "order": 3})
    assert r.status_code == 302
    page.refresh_from_db()
    assert page.title == "House rules v2" and page.published is False and page.show_on_dashboard is False
    log = AuditLog.objects.filter(action="update", target_id=str(page.pk)).get()
    assert log.changes["title"] == ["House rules", "House rules v2"] and log.changes["body"] == ["…", "…"]
    assert emit.call_args.args[1]["action"] == "update"

    # delete
    with mock.patch("apps.pages.views.emit") as emit:
        r = c.post(_url("delete", "rules"))
    assert r.status_code == 302 and not InfoPage.objects.filter(pk=page.pk).exists()
    assert AuditLog.objects.filter(action="delete", target_id=str(page.pk)).exists()
    emit.assert_not_called()


def test_create_rejects_duplicate_and_reserved_slug(orga, page):
    c = _client(orga)
    r = c.post(_url("create"), {"title": "Dup", "slug": "how-to-dect", "body": "", "order": 0})
    assert r.status_code == 200 and "already exists" in r.content.decode()
    r = c.post(_url("create"), {"title": "Bad", "slug": "manage", "body": "", "order": 0})
    assert r.status_code == 200 and "reserved" in r.content.decode()
    assert InfoPage.objects.count() == 1


def test_edit_unknown_page_404(orga):
    assert _client(orga).get(_url("edit", "nope")).status_code == 404


def test_page_of_other_event_is_isolated(orga, page, admin):
    import datetime as dt

    from apps.events.models import Event

    other = Event.objects.create(name="Other", slug="other", start_date=dt.date.today(), end_date=dt.date.today(),
                                 state=Event.State.LIVE)
    InfoPage.objects.create(event=other, slug="theirs", title="Theirs", body="x")
    c = _client(orga)
    assert c.get(_url("show", "theirs")).status_code == 404
    assert c.get(reverse("pages:manage", args=["other"])).status_code == 403


# --------------------------------------------------------------------------- dashboard tag

def test_dashboard_lists_flagged_pages(member, user, page, draft, event):
    InfoPage.objects.create(event=event, slug="hidden", title="Not on dashboard", body="x", show_on_dashboard=False)
    InfoPage.objects.create(event=event, slug="flag-draft", title="Flagged draft", body="x", show_on_dashboard=True,
                            published=False)
    body = _client(user).get(reverse("portal:event_dashboard", args=["demo"])).content.decode()
    assert "DECT how-to" in body and "Step 1" in body  # title + first line
    assert "Not on dashboard" not in body and "Flagged draft" not in body
    assert _url("index") in body and _url("show", "how-to-dect") in body


def test_dashboard_orga_sees_flagged_drafts(orga, event):
    InfoPage.objects.create(event=event, slug="flag-draft", title="Flagged draft", body="x", show_on_dashboard=True,
                            published=False)
    body = _client(orga).get(reverse("portal:event_dashboard", args=["demo"])).content.decode()
    assert "Flagged draft" in body


def test_dashboard_without_pages_renders_nothing(member, user, event):
    body = _client(user).get(reverse("portal:event_dashboard", args=["demo"])).content.decode()
    assert "dashboard-pages" not in body


def test_dashboard_tag_directly(event, page, user):
    from django.template import Context, Template

    html = Template("{% load pages_tags %}{% dashboard_pages event %}").render(Context({"event": event, "user": user}))
    assert "DECT how-to" in html and "Step 1" in html
