"""Call group portal views and REST API."""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.callgroups import services
from apps.callgroups.models import CallGroup, GroupMember

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.callback.tests.urls_stub")]


def _url(name, event, *args):
    return reverse(f"callgroups:{name}", args=[event.slug, *args])


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_index_requires_login_and_lists(client, event, user, member, full_group):
    r = client.get(_url("index", event))
    assert r.status_code == 302 and "login" in r["Location"]
    client.force_login(user)
    r = client.get(_url("index", event))
    assert r.status_code == 200
    body = r.content.decode()
    assert "Infodesk" in body and "4400" in body and "3 / 3" in body and "*71" in body
    assert "Log out" in body  # toggle button for my own membership


def test_create_view(client, event, user, member):
    client.force_login(user)
    r = client.get(_url("create", event))
    assert r.status_code == 200
    assert 'data-availability-url="/api/v1/availability/?event=demo&amp;number="' in r.content.decode()
    assert 'id="availability" class="avail"' in r.content.decode()
    r = client.post(_url("create", event), {"number": "4400", "name": "Infodesk", "strategy": "ringall",
                                            "ring_timeout": 20, "wrap_up_seconds": 0, "allow_self_service": "on",
                                            "in_phonebook": "on"})
    assert r.status_code == 302
    g = CallGroup.objects.get()
    assert g.number == "4400" and g.extension.owner == user
    # taken number -> form error re-rendered
    r = client.post(_url("create", event), {"number": "4400", "name": "Dup", "strategy": "ringall",
                                            "ring_timeout": 20, "wrap_up_seconds": 0})
    assert r.status_code == 200 and "already taken" in r.content.decode()


def test_detail_manage_and_toggle_permissions(client, event, user, other_user, orga, member, group, ext_alice, ext_bob):
    from apps.events.models import EventMembership

    EventMembership.objects.create(event=event, user=other_user, role="user")
    # owner adds members
    client.force_login(user)
    assert client.post(_url("add_member", event, group.pk), {"number": "4242"}).status_code == 302
    assert client.post(_url("add_member", event, group.pk), {"number": "4300"}).status_code == 302
    assert client.post(_url("add_member", event, group.pk), {"number": "1"}).status_code == 302  # unknown -> message
    assert group.members.count() == 2
    r = client.get(_url("detail", event, group.pk))
    assert r.status_code == 200 and "Add member" in r.content.decode() and "4300" in r.content.decode()
    # owner changes strategy via the detail form
    r = client.post(_url("detail", event, group.pk), {"name": "Infodesk", "strategy": "longestidle", "ring_timeout": 25,
                                                      "wrap_up_seconds": 10, "allow_self_service": "on",
                                                      "description": ""})
    assert r.status_code == 302
    group.refresh_from_db()
    assert group.strategy == "longestidle" and group.ring_timeout == 25

    bob = group.members.get(extension__number="4300")
    alice = group.members.get(extension__number="4242")
    # bob may toggle himself but not alice, and may not add members
    client.force_login(other_user)
    assert client.post(_url("toggle", event, group.pk, bob.pk, "logout")).status_code == 302
    bob.refresh_from_db()
    assert bob.logged_in is False
    assert client.post(_url("toggle", event, group.pk, alice.pk, "logout")).status_code == 403
    assert client.post(_url("add_member", event, group.pk), {"number": "4301"}).status_code == 403
    r = client.get(_url("detail", event, group.pk))
    assert r.status_code == 200 and "Add member" not in r.content.decode()
    # orga can do everything
    client.force_login(orga)
    assert client.post(_url("toggle", event, group.pk, bob.pk, "login")).status_code == 302
    assert client.post(_url("remove_member", event, group.pk, bob.pk)).status_code == 302
    assert not GroupMember.objects.filter(pk=bob.pk).exists()
    # self-service off -> member can't toggle
    services.update_group(group, orga, allow_self_service=False)
    client.force_login(user)
    assert client.post(_url("toggle", event, group.pk, alice.pk, "logout")).status_code == 302  # owner still may
    client.force_login(other_user)
    services.add_member(group, ext_bob, orga)
    bob = group.members.get(extension__number="4300")
    assert client.post(_url("toggle", event, group.pk, bob.pk, "logout")).status_code == 403
    # delete
    client.force_login(user)
    assert client.post(_url("delete", event, group.pk)).status_code == 302
    assert not CallGroup.objects.filter(pk=group.pk).exists()


def test_feature_flag_off(client, event, user, member, settings):
    settings.PET_FEATURES = dict(settings.PET_FEATURES, callgroups=False)
    client.force_login(user)
    assert client.get(_url("index", event)).status_code == 404


def test_api_crud_and_actions(event, user, other_user, orga, member, ext_alice, ext_bob):
    c = _client(user)
    r = c.post("/api/v1/callgroups/", {"event": "demo", "number": "4400", "name": "Infodesk", "strategy": "roundrobin"})
    assert r.status_code == 201, r.content
    gid = r.json()["id"]
    assert r.json()["number"] == "4400" and r.json()["owner"] == "alice" and r.json()["members_total"] == 0
    assert c.post("/api/v1/callgroups/", {"event": "demo", "number": "4400", "name": "x"}).status_code == 400

    assert c.get("/api/v1/callgroups/?event=demo").json()["count"] == 1
    r = c.post(f"/api/v1/callgroups/{gid}/add-member/", {"number": "4242"})
    assert r.status_code == 201 and r.json()["number"] == "4242"
    assert c.post(f"/api/v1/callgroups/{gid}/add-member/", {"number": "4300", "priority": 1}).status_code == 201
    assert c.post(f"/api/v1/callgroups/{gid}/add-member/", {"number": "0000"}).status_code == 400
    assert len(c.get(f"/api/v1/callgroups/{gid}/members/").json()) == 2
    assert c.get(f"/api/v1/callgroups/{gid}/targets/").json() == {
        "group": "4400", "strategy": "roundrobin", "serial": True, "targets": ["4242", "4300"],
        "waves": [{"delay": 0, "targets": ["4242", "4300"]}], "callerid_prefix": ""}

    # bob logs himself out (no number needed), can't touch alice, can't edit
    b = _client(other_user)
    r = b.post(f"/api/v1/callgroups/{gid}/logout/", {})
    assert r.status_code == 200 and r.json()[0]["logged_in"] is False
    assert b.post(f"/api/v1/callgroups/{gid}/logout/", {"number": "4242"}).status_code == 403
    assert b.patch(f"/api/v1/callgroups/{gid}/", {"strategy": "ringall"}).status_code == 403
    assert c.get(f"/api/v1/callgroups/{gid}/targets/").json()["targets"] == ["4242"]
    assert c.post(f"/api/v1/callgroups/{gid}/login/", {"number": "4300"}).status_code == 200

    r = c.patch(f"/api/v1/callgroups/{gid}/", {"strategy": "ringall", "name": "Front"})
    assert r.status_code == 200 and r.json()["strategy"] == "ringall" and r.json()["name"] == "Front"
    assert c.post(f"/api/v1/callgroups/{gid}/remove-member/", {"number": "4300"}).status_code == 204
    assert _client(orga).delete(f"/api/v1/callgroups/{gid}/").status_code == 204
    assert not CallGroup.objects.filter(pk=gid).exists()
