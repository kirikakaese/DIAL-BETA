"""Portal views and REST API of the GURU3-style workflow: admins, invites, my memberships, member settings."""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.callgroups import services
from apps.callgroups.models import CallGroupInvite, GroupMember

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.callback.tests.urls_stub")]


def _url(name, event, *args):
    return reverse(f"callgroups:{name}", args=[event.slug, *args])


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def bob_member(event, other_user):
    from apps.events.models import EventMembership

    return EventMembership.objects.create(event=event, user=other_user, role="user")


def test_admin_views(client, event, user, other_user, orga, member, bob_member, group):
    client.force_login(user)
    r = client.get(_url("detail", event, group.pk))
    assert r.status_code == 200 and "Group admins" in r.content.decode()
    assert client.post(_url("add_admin", event, group.pk), {"identifier": "bob@example.org"}).status_code == 302
    assert list(group.admins.all()) == [other_user]
    assert client.post(_url("add_admin", event, group.pk), {"identifier": "nobody"}).status_code == 302  # message
    assert group.admins.count() == 1
    # bob manages members now, but sees no admin section and may not appoint admins
    client.force_login(other_user)
    r = client.get(_url("detail", event, group.pk))
    body = r.content.decode()
    assert "Add member" in body and "Invite extension" in body and "Group admins" not in body
    assert client.post(_url("add_admin", event, group.pk), {"identifier": "orga"}).status_code == 403
    assert client.post(_url("remove_admin", event, group.pk, other_user.pk)).status_code == 403
    # orga removes bob
    client.force_login(orga)
    assert client.post(_url("remove_admin", event, group.pk, other_user.pk)).status_code == 302
    assert group.admins.count() == 0


def test_invite_views(client, event, user, other_user, member, bob_member, group, ext_bob, ext_alice, mailoutbox):
    client.force_login(user)
    r = client.post(_url("invite", event, group.pk), {"number": "4300", "reason": "please"})
    assert r.status_code == 302 and len(mailoutbox) == 1
    inv = CallGroupInvite.objects.get()
    assert client.post(_url("invite", event, group.pk), {"number": "0000"}).status_code == 302  # unknown -> message
    assert client.post(_url("invite", event, group.pk), {"number": "4300"}).status_code == 302  # dup -> message
    assert CallGroupInvite.objects.count() == 1
    body = client.get(_url("detail", event, group.pk)).content.decode()
    assert "4300" in body and "please" in body and "Cancel" in body
    # alice (inviter) may not answer bob's invite
    assert client.get(_url("invite_respond", event, inv.token)).status_code == 403
    assert client.post(_url("invite_respond", event, inv.token), {"action": "accept"}).status_code == 403
    assert client.get(_url("invites", event)).status_code == 200
    assert "No open invitations" in client.get(_url("invites", event)).content.decode()
    # bob sees it on index / mine / invites, opens the mail link and accepts
    client.force_login(other_user)
    assert "open call group invitation" in client.get(_url("index", event)).content.decode()
    assert "Infodesk" in client.get(_url("invites", event)).content.decode()
    r = client.get(_url("invite_respond", event, inv.token))
    assert r.status_code == 200 and "Accept - join the group" in r.content.decode()
    r = client.post(_url("invite_respond", event, inv.token), {"action": "accept"})
    assert r.status_code == 302 and r["Location"] == _url("mine", event)
    assert group.members.filter(extension=ext_bob).exists()
    r = client.get(_url("invite_respond", event, inv.token))
    assert r.status_code == 200 and "no longer open" in r.content.decode()
    # decline flow + cancel by the manager
    inv2 = services.invite(group, ext_alice, user)
    client.force_login(user)  # alice owns 4242 -> may decline
    r = client.post(_url("invite_respond", event, inv2.token), {"action": "decline", "next": _url("index", event)})
    assert r.status_code == 302 and r["Location"] == _url("index", event)
    inv2.refresh_from_db()
    assert inv2.status == "declined"
    inv3 = services.invite(group, ext_alice, user)
    client.force_login(other_user)
    assert client.post(_url("cancel_invite", event, group.pk, inv3.pk)).status_code == 403
    client.force_login(user)
    assert client.post(_url("cancel_invite", event, group.pk, inv3.pk)).status_code == 302
    inv3.refresh_from_db()
    assert inv3.status == "cancelled"
    assert client.get(_url("invite_respond", event, "no-such-token")).status_code == 404


def test_mine_leave_and_member_settings(client, event, user, other_user, member, bob_member, full_group):
    bob = full_group.members.get(extension__number="4300")
    client.force_login(other_user)
    r = client.get(_url("mine", event))
    assert r.status_code == 200 and "4300" in r.content.decode() and "Leave" in r.content.decode()
    # bob may not change settings, and may not leave with alice's extension
    alice = full_group.members.get(extension__number="4242")
    assert client.post(_url("member_settings", event, full_group.pk, bob.pk), {"delay_s": 5}).status_code == 403
    assert client.post(_url("leave", event, full_group.pk, alice.pk)).status_code == 403
    r = client.post(_url("leave", event, full_group.pk, bob.pk))
    assert r.status_code == 302 and r["Location"] == _url("mine", event)
    assert not GroupMember.objects.filter(pk=bob.pk).exists()
    # owner sets priority + delay via the inline form
    client.force_login(user)
    r = client.post(_url("member_settings", event, full_group.pk, alice.pk), {"priority": 3, "delay_s": 5})
    assert r.status_code == 302
    alice.refresh_from_db()
    assert alice.priority == 3 and alice.delay_s == 5
    assert client.post(_url("member_settings", event, full_group.pk, alice.pk), {"delay_s": 999}).status_code == 302
    alice.refresh_from_db()
    assert alice.delay_s == 5  # invalid -> unchanged (message)
    body = client.get(_url("detail", event, full_group.pk)).content.decode()
    assert "Ring waves" in body and "+5 s" in body
    # add member with a delay / shortcode via settings form
    assert client.post(_url("add_member", event, full_group.pk), {"number": "4300", "delay_s": 8}).status_code == 302
    assert full_group.members.get(extension__number="4300").delay_s == 8
    r = client.post(_url("detail", event, full_group.pk), {
        "name": "Infodesk", "strategy": "ringall", "ring_timeout": 20, "wrap_up_seconds": 0,
        "allow_self_service": "on", "description": "", "shortcode": "INF"})
    assert r.status_code == 302
    full_group.refresh_from_db()
    assert full_group.shortcode == "INF" and full_group.callerid_prefix == "[INF] "


def test_create_view_with_shortcode(client, event, user, member):
    client.force_login(user)
    r = client.post(_url("create", event), {"number": "4400", "name": "Security", "strategy": "ringall",
                                            "ring_timeout": 20, "wrap_up_seconds": 0, "shortcode": "SEC"})
    assert r.status_code == 302
    assert services.group_for_number(event, "4400").shortcode == "SEC"


def test_api_invites_and_admins(event, user, other_user, orga, member, bob_member, ext_alice, ext_bob, mailoutbox):
    c = _client(user)
    r = c.post("/api/v1/callgroups/", {"event": "demo", "number": "4400", "name": "Infodesk", "shortcode": "INF"})
    assert r.status_code == 201, r.content
    gid = r.json()["id"]
    assert r.json()["shortcode"] == "INF" and r.json()["admins"] == []
    # admins
    b = _client(other_user)
    assert b.post(f"/api/v1/callgroups/{gid}/admins/", {"user": "bob"}).status_code == 403
    r = c.post(f"/api/v1/callgroups/{gid}/admins/", {"user": "bob@example.org"})
    assert r.status_code == 200 and r.json() == ["bob"]
    assert c.post(f"/api/v1/callgroups/{gid}/admins/", {"user": "nobody"}).status_code == 400
    assert c.get(f"/api/v1/callgroups/{gid}/").json()["admins"] == ["bob"]
    # bob (admin) may now invite alice's extension; alice sees + accepts it
    r = b.post(f"/api/v1/callgroups/{gid}/invite/", {"number": "4242", "reason": "hi"})
    assert r.status_code == 201 and r.json()["status"] == "open" and len(mailoutbox) == 1
    iid = r.json()["id"]
    assert b.post(f"/api/v1/callgroups/{gid}/invite/", {"number": "4242"}).status_code == 400
    assert b.post(f"/api/v1/callgroups/{gid}/invite/", {"number": "0000"}).status_code == 400
    assert [i["id"] for i in b.get(f"/api/v1/callgroups/{gid}/invites/").json()] == [iid]
    assert b.get("/api/v1/callgroups/invites/?event=demo").json() == []  # none for bob's extensions
    assert [i["id"] for i in c.get("/api/v1/callgroups/invites/?event=demo").json()] == [iid]
    assert b.post(f"/api/v1/callgroups/invites/{iid}/respond/", {"accept": True}).status_code == 403
    r = c.post(f"/api/v1/callgroups/invites/{iid}/respond/", {"accept": "true"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert c.get(f"/api/v1/callgroups/{gid}/members/").json()[0]["number"] == "4242"
    assert c.post(f"/api/v1/callgroups/invites/{iid}/respond/", {"accept": False}).status_code == 400  # answered
    # cancel + member delay + waves
    iid2 = c.post(f"/api/v1/callgroups/{gid}/invite/", {"number": "4300"}).json()["id"]
    assert c.post(f"/api/v1/callgroups/{gid}/invites/{iid2}/cancel/").json()["status"] == "cancelled"
    o = _client(orga)
    assert o.post(f"/api/v1/callgroups/{gid}/add-member/", {"number": "4300", "delay_s": 4}).status_code == 201
    t = c.get(f"/api/v1/callgroups/{gid}/targets/").json()
    assert t["waves"] == [{"delay": 0, "targets": ["4242"]}, {"delay": 4, "targets": ["4300"]}]
    assert t["callerid_prefix"] == "[INF] "
    m = c.get(f"/api/v1/callgroups/{gid}/members/").json()
    assert {x["number"]: x["delay_s"] for x in m} == {"4242": 0, "4300": 4} and m[0]["is_group"] is False
    r = c.delete(f"/api/v1/callgroups/{gid}/admins/", {"user": "bob"})
    assert r.status_code == 200 and r.json() == []
