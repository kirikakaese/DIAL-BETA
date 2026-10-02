"""Info pages REST API: list/retrieve for members, CRUD for orga, isolation between events."""
from unittest import mock

import pytest
from rest_framework.test import APIClient

from apps.core.models import AuditLog
from apps.pages.models import InfoPage

pytestmark = pytest.mark.django_db

URL = "/api/v1/pages/"


@pytest.fixture
def page(event, orga):
    return InfoPage.objects.create(event=event, slug="how-to-dect", title="DECT how-to", body="Dial **9002**.",
                                   order=1, show_on_dashboard=True, updated_by=orga)


@pytest.fixture
def draft(event):
    return InfoPage.objects.create(event=event, slug="draft", title="Secret draft", body="wip", published=False,
                                   order=2)


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_list_requires_event_param(member, user, page):
    r = _client(user).get(URL)
    assert r.status_code == 400 and "event" in r.json()


def test_member_lists_published_and_gets_body_html(member, user, page, draft):
    r = _client(user).get(URL, {"event": "demo"})
    assert r.status_code == 200
    rows = r.json()["results"]
    assert [p["slug"] for p in rows] == ["how-to-dect"]
    p = rows[0]
    assert p["event"] == "demo" and p["title"] == "DECT how-to" and p["body"] == "Dial **9002**."
    assert p["body_html"] == "<p>Dial <strong>9002</strong>.</p>"
    assert p["order"] == 1 and p["published"] is True and p["show_on_dashboard"] is True and p["updated_at"]
    assert set(p) == {"id", "event", "slug", "title", "body", "body_html", "order", "published", "show_on_dashboard",
                      "updated_at"}
    assert _client(user).get(f"{URL}{page.pk}/").status_code == 200
    assert _client(user).get(f"{URL}{draft.pk}/").status_code == 404
    assert _client(user).get(f"{URL}{draft.pk}/", {"event": "demo"}).status_code == 404


def test_orga_lists_drafts_too(orga, page, draft):
    r = _client(orga).get(URL, {"event": "demo"})
    assert [p["slug"] for p in r.json()["results"]] == ["how-to-dect", "draft"]
    assert _client(orga).get(f"{URL}{draft.pk}/").status_code == 200


def test_anonymous_and_unknown_event(page):
    assert APIClient().get(URL, {"event": "demo"}).status_code in (401, 403)
    assert _client(page.updated_by).get(URL, {"event": "nope"}).status_code == 404


def test_member_cannot_write(member, user, page):
    c = _client(user)
    body = {"event": "demo", "slug": "rules", "title": "Rules", "body": "x"}
    assert c.post(URL, body, format="json").status_code == 403
    assert c.patch(f"{URL}{page.pk}/", {"title": "hacked"}, format="json").status_code == 403
    assert c.delete(f"{URL}{page.pk}/").status_code == 403
    page.refresh_from_db()
    assert page.title == "DECT how-to" and InfoPage.objects.count() == 1


def test_orga_crud_with_audit_and_webhook(orga, event):
    c = _client(orga)
    with mock.patch("apps.pages.views.emit") as emit:
        r = c.post(URL, {"event": "demo", "slug": "rules", "title": "Rules", "body": "<b>Be</b> *nice*",
                         "order": 5, "published": False, "show_on_dashboard": True}, format="json")
    assert r.status_code == 201, r.content
    d = r.json()
    assert d["body_html"] == "<p>&lt;b&gt;Be&lt;/b&gt; <em>nice</em></p>"
    assert d["published"] is False and d["order"] == 5
    page = InfoPage.objects.get(pk=d["id"])
    assert page.updated_by == orga
    assert AuditLog.objects.filter(action="create", target_id=str(page.pk), actor=orga).exists()
    assert emit.call_args.args[0] == "page.updated" and emit.call_args.args[1]["action"] == "create"

    with mock.patch("apps.pages.views.emit") as emit:
        r = c.patch(f"{URL}{page.pk}/", {"title": "Rules v2", "published": True}, format="json")
    assert r.status_code == 200 and r.json()["title"] == "Rules v2" and r.json()["published"] is True
    log = AuditLog.objects.get(action="update", target_id=str(page.pk))
    assert set(log.changes) == {"title", "published"}
    assert emit.call_args.args[1]["action"] == "update"

    r = c.put(f"{URL}{page.pk}/", {"event": "demo", "slug": "rules-2", "title": "Rules v3", "body": "y"},
              format="json")
    assert r.status_code == 200 and r.json()["slug"] == "rules-2"

    with mock.patch("apps.pages.views.emit") as emit:
        assert c.delete(f"{URL}{page.pk}/").status_code == 204
    assert not InfoPage.objects.filter(pk=page.pk).exists()
    assert AuditLog.objects.filter(action="delete", target_id=str(page.pk)).exists()
    emit.assert_not_called()


def test_validation_duplicate_reserved_and_move(orga, page, event):
    c = _client(orga)
    r = c.post(URL, {"event": "demo", "slug": "how-to-dect", "title": "Dup", "body": ""}, format="json")
    assert r.status_code == 400 and "slug" in r.json()
    r = c.post(URL, {"event": "demo", "slug": "manage", "title": "Bad", "body": ""}, format="json")
    assert r.status_code == 400 and "slug" in r.json()
    import datetime as dt

    from apps.events.models import Event

    other = Event.objects.create(name="Other", slug="other", start_date=dt.date.today(), end_date=dt.date.today())
    r = c.patch(f"{URL}{page.pk}/", {"event": "other"}, format="json")
    assert r.status_code == 400 and "event" in r.json()
    assert other.pages.count() == 0


def test_orga_of_one_event_cannot_touch_another(orga, page, admin):
    import datetime as dt

    from apps.events.models import Event

    other = Event.objects.create(name="Other", slug="other", start_date=dt.date.today(), end_date=dt.date.today(),
                                 state=Event.State.LIVE)
    theirs = InfoPage.objects.create(event=other, slug="theirs", title="Theirs", body="x")
    c = _client(orga)
    assert c.post(URL, {"event": "other", "slug": "x", "title": "X", "body": ""}, format="json").status_code == 403
    assert c.patch(f"{URL}{theirs.pk}/", {"title": "hacked"}, format="json").status_code == 403
    assert c.delete(f"{URL}{theirs.pk}/").status_code == 403
    # ...but may read it, the other event is public and live
    assert c.get(f"{URL}{theirs.pk}/").status_code == 200
    # superuser sees everything
    r = _client(admin).get(URL, {"event": "other"})
    assert r.status_code == 200 and r.json()["count"] == 1
