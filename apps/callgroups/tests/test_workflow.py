"""GURU3-style call group workflow: group admins, invites, leaving, nested groups, ring delays, shortcodes."""
import pytest
from django.core.exceptions import PermissionDenied

from apps.callgroups import services
from apps.callgroups.export import export_event
from apps.callgroups.models import CallGroupInvite, GroupLoginLog, GroupMember
from apps.callgroups.services import CallGroupError
from apps.core.models import AuditLog
from apps.extensions.services import register

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- admins

def test_can_manage_with_admins(group, user, other_user, orga, admin):
    assert services.can_manage(user, group)  # owner
    assert services.can_manage(orga, group)
    assert services.can_manage(admin, group)  # superuser counts as orga
    assert not services.can_manage(other_user, group)
    assert services.add_admin(group, other_user, user) is True
    assert services.add_admin(group, other_user, user) is False  # idempotent
    assert services.can_manage(other_user, group)
    assert not services.is_owner_or_orga(other_user, group)
    assert AuditLog.objects.filter(message="Group admin bob added").exists()
    # admins may not appoint further admins; the owner cannot be added
    with pytest.raises(PermissionDenied):
        services.add_admin(group, orga, other_user)
    with pytest.raises(CallGroupError):
        services.add_admin(group, user, user)
    with pytest.raises(CallGroupError):
        services.add_admin(group, None, user)
    assert services.remove_admin(group, other_user, orga) is True
    assert not services.can_manage(other_user, group)
    assert services.find_user("BOB@example.org") == other_user and services.find_user("Bob") == other_user
    assert services.find_user("") is None


def test_export_includes_admins_shortcode_delay(group, user, other_user, ext_alice, event):
    services.add_admin(group, other_user, user)
    services.update_group(group, user, shortcode="inf")
    services.add_member(group, ext_alice, user, delay_s=7)
    data = export_event(event)["callgroups"][0]
    assert data["admins"] == ["bob"] and data["shortcode"] == "inf"
    assert data["members"] == [{"number": "4242", "logged_in": True, "priority": 0, "delay_s": 7}]


# --------------------------------------------------------------------------- invites

def test_invite_accept_flow(group, ext_bob, user, other_user, event, settings, mailoutbox):
    settings.PET_PUBLIC_URL = "https://pet.example.org/"
    inv = services.invite(group, ext_bob, user, "we need you at the desk")
    assert inv.is_open and inv.status == "open" and inv.token
    assert len(mailoutbox) == 1
    msg = mailoutbox[0]
    assert msg.to == ["bob@example.org"] and "4400" in msg.subject
    assert f"https://pet.example.org/e/demo/callgroups/invites/{inv.token}/" in msg.body
    assert "we need you at the desk" in msg.body
    assert list(services.open_invites_for(event, other_user)) == [inv]
    assert not services.open_invites_for(event, user).exists()
    # duplicate open invite / already a member -> error
    with pytest.raises(CallGroupError):
        services.invite(group, ext_bob, user)
    # only the extension owner (or orga) may answer
    with pytest.raises(PermissionDenied):
        services.respond(inv, user, True)
    services.respond(inv, other_user, True)
    inv.refresh_from_db()
    assert inv.accepted is True and not inv.is_open and inv.status == "accepted"
    m = group.members.get(extension=ext_bob)
    assert m.logged_in and GroupLoginLog.objects.filter(member=m, action="added", via="invite").exists()
    with pytest.raises(CallGroupError):
        services.respond(inv, other_user, True)  # already answered
    with pytest.raises(CallGroupError):
        services.invite(group, ext_bob, user)  # now a member
    assert AuditLog.objects.filter(message__contains="accepted the invitation").exists()


def test_invite_decline_cancel_and_permissions(group, ext_bob, ext_alice, user, other_user, orga, event, mailoutbox):
    with pytest.raises(PermissionDenied):
        services.invite(group, ext_bob, other_user)  # not a manager
    with pytest.raises(CallGroupError):
        services.invite(group, group.extension, user)  # not an endpoint
    inv = services.invite(group, ext_bob, orga)
    services.respond(inv, orga, False)  # orga may answer on behalf of the owner
    inv.refresh_from_db()
    assert inv.accepted is False and inv.status == "declined"
    assert not group.members.filter(extension=ext_bob).exists()
    # a declined invite does not block a new one (partial unique constraint)
    inv2 = services.invite(group, ext_bob, user)
    assert inv2.pk != inv.pk and CallGroupInvite.objects.filter(group=group, extension=ext_bob).count() == 2
    with pytest.raises(PermissionDenied):
        services.cancel_invite(inv2, other_user)
    services.cancel_invite(inv2, user)
    inv2.refresh_from_db()
    assert inv2.status == "cancelled" and inv2.accepted is None and not inv2.is_open
    # an extension without an owner e-mail gets no mail but the invite still exists
    ext_alice.owner.email = ""
    ext_alice.owner.save()
    n = len(mailoutbox)
    services.invite(group, ext_alice, user)
    assert len(mailoutbox) == n


def test_invite_emits_webhook(group, ext_bob, user, monkeypatch):
    seen = []
    monkeypatch.setattr(services, "emit", lambda t, p, event=None: seen.append((t, p)))
    inv = services.invite(group, ext_bob, user, "r")
    services.respond(inv, ext_bob.owner, True)
    assert [t for t, _p in seen] == ["callgroup.invited", "callgroup.invite_accepted"]
    assert seen[0][1]["extension"] == "4300" and seen[0][1]["group"] == "4400" and seen[0][1]["reason"] == "r"


# --------------------------------------------------------------------------- leave

def test_leave(full_group, user, other_user, orga):
    bob = full_group.members.get(extension__number="4300")
    with pytest.raises(PermissionDenied):
        services.leave(bob, user)  # group owner is not bob's extension owner -> use remove_member
    with pytest.raises(PermissionDenied):
        services.leave(bob, orga)
    services.leave(bob, other_user)
    assert not GroupMember.objects.filter(pk=bob.pk).exists()
    assert services.dial_targets(full_group.extension) == ["4242", "4301"]


# --------------------------------------------------------------------------- nested groups

@pytest.fixture
def nested(event, user, other_user, orga, ext_alice, ext_bob, ext_carol):
    """4400 Infodesk -> 4410 Security (nested) -> 4420 Medics (nested)."""
    top = services.create_group(event, user, "4400", "Infodesk", "ringall")
    sec = services.create_group(event, orga, "4410", "Security", "ringall", shortcode="SEC")
    med = services.create_group(event, orga, "4420", "Medics", "ringall")
    services.add_member(top, ext_alice, user)
    services.add_member(top, sec.extension, user)
    services.add_member(sec, ext_bob, orga)
    services.add_member(sec, med.extension, orga)
    services.add_member(med, ext_carol, orga)
    return top, sec, med


def test_nested_resolution(nested, ext_bob, orga):
    top, sec, med = nested
    assert services.dial_targets(top.extension) == ["4242", "4300", "4301"]
    assert services.dial_targets(sec.extension) == ["4300", "4301"]
    # logging out of the nested group removes them from the parent as well
    services.logout(sec.members.get(extension=ext_bob), "web")
    assert services.dial_targets(top.extension) == ["4242", "4301"]
    # a logged-out nested group membership hides the whole subtree
    services.logout(top.members.get(extension=sec.extension), "web")
    assert services.dial_targets(top.extension) == ["4242"]
    # an endpoint reachable twice is listed once
    services.login(top.members.get(extension=sec.extension), "web")
    services.add_member(top, ext_bob, orga)
    services.login(sec.members.get(extension=ext_bob), "web")
    assert services.dial_targets(top.extension) == ["4242", "4300", "4301"]


def test_nested_cycle_rejected(nested, user, orga):
    top, sec, med = nested
    with pytest.raises(CallGroupError, match="loop"):
        services.add_member(med, top.extension, orga)  # med -> top -> sec -> med
    with pytest.raises(CallGroupError, match="loop"):
        services.add_member(sec, top.extension, orga)
    with pytest.raises(CallGroupError, match="itself"):
        services.add_member(top, top.extension, user)
    # a plain group extension without a CallGroup row is rejected too
    orphan = register(event=top.event, user=orga, number="4499", extension_type="group")
    with pytest.raises(CallGroupError, match="not a managed"):
        services.add_member(top, orphan, orga)


def test_nested_depth_cap(nested, event, orga):
    top, sec, med = nested
    deep = services.create_group(event, orga, "4430", "Deep", "ringall")
    deeper = services.create_group(event, orga, "4440", "Deeper", "ringall")
    d1 = register(event, orga, "4431", "sip")
    d2 = register(event, orga, "4441", "sip")
    services.add_member(deep, d1, orga)
    services.add_member(deeper, d2, orga)
    services.add_member(med, deep.extension, orga)      # depth 3 from top -> still resolved
    services.add_member(deep, deeper.extension, orga)   # depth 4 from top -> cut off
    assert services.dial_targets(top.extension) == ["4242", "4300", "4301", "4431"]
    assert services.dial_targets(sec.extension) == ["4300", "4301", "4431", "4441"]  # depth 3 from sec


def test_nested_group_survives_resolution_when_cycle_appears_in_db(nested, orga):
    """Belt and braces: even if a cycle sneaks into the DB, resolution terminates."""
    top, sec, med = nested
    GroupMember.objects.create(group=med, extension=top.extension)
    assert services.dial_targets(top.extension) == ["4242", "4300", "4301"]


# --------------------------------------------------------------------------- ring delays / waves

def test_dial_waves_ordering_by_delay(full_group, user):
    members = {m.extension.number: m for m in full_group.members.select_related("extension")}
    services.update_member(members["4242"], user, delay_s=10)
    services.update_member(members["4301"], user, delay_s=5)
    assert services.dial_waves(full_group.extension) == [
        {"delay": 0, "targets": ["4300"]}, {"delay": 5, "targets": ["4301"]}, {"delay": 10, "targets": ["4242"]}]
    # dial_targets keeps the same order so the plain dial string degrades sensibly
    assert services.dial_targets(full_group.extension) == ["4300", "4301", "4242"]
    # serial strategies ignore delays: a single wave in ring order
    services.update_group(full_group, user, strategy="roundrobin")
    assert services.dial_waves(full_group.extension) == [{"delay": 0, "targets": ["4242", "4300", "4301"]}]
    assert AuditLog.objects.filter(message="Member 4242 updated", changes={"delay_s": [0, 10]}).exists()


def test_dial_waves_nested_offsets(nested, orga, user):
    top, sec, med = nested
    services.update_member(top.members.get(extension=sec.extension), user, delay_s=5)
    services.update_member(sec.members.get(extension=med.extension), orga, delay_s=3)
    assert services.dial_waves(top.extension) == [
        {"delay": 0, "targets": ["4242"]}, {"delay": 5, "targets": ["4300"]}, {"delay": 8, "targets": ["4301"]}]
    assert services.dial_waves(sec.extension) == [{"delay": 0, "targets": ["4300"]}, {"delay": 3, "targets": ["4301"]}]
    # duplicate reachable via a faster path keeps the smaller delay
    services.add_member(top, med.extension, user, delay_s=1)
    assert services.dial_waves(top.extension) == [
        {"delay": 0, "targets": ["4242"]}, {"delay": 1, "targets": ["4301"]}, {"delay": 5, "targets": ["4300"]}]


def test_dial_waves_degrades(event, ext_alice, settings):
    assert services.dial_waves(ext_alice) == [] and services.dial_waves(None) == []
    settings.PET_FEATURES = dict(settings.PET_FEATURES, callgroups=False)
    assert services.dial_waves(ext_alice) == []


def test_delayed_dial_target_encoding():
    assert services.delayed_dial_target("4300", 5) == "Local/005*4300@pet-group"
    assert services.delayed_dial_target("4300", 120) == "Local/120*4300@pet-group"


# --------------------------------------------------------------------------- shortcode

def test_shortcode_prefix(event, user, member, ext_alice):
    g = services.create_group(event, user, "4400", "Security", "ringall", shortcode=" sec ")
    assert g.shortcode == "sec" and g.callerid_prefix == "[sec] "
    assert services.callerid_prefix(g.extension) == "[sec] "
    assert services.callerid_prefix(ext_alice) == ""
    services.update_group(g, user, shortcode="")
    assert g.callerid_prefix == "" and services.callerid_prefix(g.extension) == ""
    services.update_group(g, user, shortcode="MEDICSXXXX")
    assert g.shortcode == "MEDICSXX"  # truncated to the field length
