"""Call group services: creation, membership, strategies, feature codes, export."""
import datetime as dt

import pytest
from django.utils import timezone

from apps.callgroups import services
from apps.callgroups.export import export_event
from apps.callgroups.models import CallGroup, GroupLoginLog, GroupMember
from apps.callgroups.services import CallGroupError
from apps.extensions.models import Extension
from apps.extensions.services import register

pytestmark = pytest.mark.django_db


def test_create_group_registers_group_extension(event, user, member):
    g = services.create_group(event, user, "4400", "Infodesk", "roundrobin", description="Front desk")
    ext = g.extension
    assert ext.type == "group" and ext.state == "active" and ext.display_name == "Infodesk"
    assert ext.owner == user and g.number == "4400" and g.name == "Infodesk"
    assert ext.ring_strategy == "serial" and ext.config["strategy"] == "roundrobin" and ext.config["members"] == []
    assert services.group_for_number(event, "4400") == g
    with pytest.raises(CallGroupError):
        services.create_group(event, user, "4400", "Dup", "ringall")  # taken
    with pytest.raises(CallGroupError):
        services.create_group(event, user, "4401", "Bad", "bogus")


def test_add_remove_member_rules(group, ext_alice, ext_bob, user, event, other_user):
    m = services.add_member(group, ext_alice, user)
    assert m.logged_in and GroupLoginLog.objects.filter(member=m, action="added").exists()
    assert services.add_member(group, ext_alice, user) == m  # idempotent
    group.extension.refresh_from_db()
    assert group.extension.config["members"] == ["4242"]
    with pytest.raises(CallGroupError):
        services.add_member(group, group.extension, user)  # itself
    other_group = services.create_group(event, other_user, "4401", "Other", "ringall")
    services.add_member(group, other_group.extension, user)  # nested group: allowed
    ann = register(event, other_user, "4402", "announcement")
    with pytest.raises(CallGroupError):
        services.add_member(group, ann, user)  # neither endpoint nor group
    services.remove_member(m, user)
    assert not GroupMember.objects.filter(pk=m.pk).exists()
    group.extension.refresh_from_db()
    assert group.extension.config["members"] == ["4401"]


def test_login_logout_and_log(full_group, user):
    m = full_group.members.get(extension__number="4242")
    services.logout(m, "web", actor=user)
    m.refresh_from_db()
    assert m.logged_in is False
    assert GroupLoginLog.objects.filter(member=m, action="logout", via="web").count() == 1
    services.logout(m, "web")  # no-op, no duplicate log
    assert GroupLoginLog.objects.filter(member=m, action="logout").count() == 1
    services.login(m, "api")
    m.refresh_from_db()
    assert m.logged_in and GroupLoginLog.objects.filter(member=m, action="login", via="api").exists()
    full_group.extension.refresh_from_db()
    assert full_group.extension.config["members"] == ["4242", "4300", "4301"]


def test_dial_targets_ring_all(full_group):
    assert services.dial_targets(full_group.extension) == ["4242", "4300", "4301"]
    services.logout(full_group.members.get(extension__number="4300"), "web")
    assert services.dial_targets(full_group.extension) == ["4242", "4301"]
    # priority wins over number order
    m = full_group.members.get(extension__number="4301")
    m.priority = 0
    m.save()
    m = full_group.members.get(extension__number="4242")
    m.priority = 5
    m.save()
    assert services.dial_targets(full_group.extension) == ["4301", "4242"]


def test_dial_targets_longest_idle(full_group, user):
    services.update_group(full_group, user, strategy="longestidle")
    now = timezone.now()
    services.record_call(full_group, "4242", at=now - dt.timedelta(minutes=1))
    services.record_call(full_group, "4300", at=now - dt.timedelta(minutes=30))
    # 4301 never took a call -> first, then 4300 (30 min idle), then 4242
    assert services.dial_targets(full_group.extension) == ["4301", "4300", "4242"]
    full_group.extension.refresh_from_db()
    assert full_group.extension.ring_strategy == "serial"


def test_dial_targets_round_robin(full_group, user):
    services.update_group(full_group, user, strategy="roundrobin")
    assert services.dial_targets(full_group.extension) == ["4242", "4300", "4301"]
    services.record_call(full_group, "4242")
    assert services.dial_targets(full_group.extension) == ["4300", "4301", "4242"]
    services.record_call(full_group, "4300")
    assert services.dial_targets(full_group.extension) == ["4301", "4242", "4300"]
    services.record_call(full_group, "4301")
    assert services.dial_targets(full_group.extension) == ["4242", "4300", "4301"]
    assert services.record_call(full_group, "9999") is None


def test_wrap_up_skips_recent_answerers(full_group, user):
    services.update_group(full_group, user, wrap_up_seconds=120)
    services.record_call(full_group, "4242")
    assert services.dial_targets(full_group.extension) == ["4300", "4301"]
    services.record_call(full_group, "4300")
    services.record_call(full_group, "4301")
    # everybody wrapping up -> still ring everyone rather than nobody
    assert sorted(services.dial_targets(full_group.extension)) == ["4242", "4300", "4301"]


def test_dial_targets_degrades(event, ext_alice, settings):
    assert services.dial_targets(ext_alice) == []  # not a group
    assert services.dial_targets(None) == []
    settings.DIAL_FEATURES = dict(settings.DIAL_FEATURES, callgroups=False)
    assert services.dial_targets(ext_alice) == []


def test_record_call_from_cdr(full_group, event):
    assert services.record_call_from_cdr(event, "4400", "4300") is not None
    assert full_group.members.get(extension__number="4300").last_call_at is not None
    assert services.record_call_from_cdr(event, "4242", "4300") is None
    assert services.record_call_from_cdr(event, "4400", None) is None


def test_feature_codes(full_group, event, user):
    # *72 + group number logs out, *71 logs back in
    assert services.handle_feature_code(event, "4242", "*72", "4400") is True
    assert full_group.members.get(extension__number="4242").logged_in is False
    assert GroupLoginLog.objects.filter(action="logout", via="feature-code").count() == 1
    assert services.handle_feature_code(event, "4242", "*71", "4400") is True
    assert full_group.members.get(extension__number="4242").logged_in is True
    # empty target toggles all my groups
    g2 = services.create_group(event, user, "4401", "Second", "ringall")
    services.add_member(g2, Extension.objects.get(number="4242", event=event), user)
    assert services.handle_feature_code(event, "4242", "*72", "") is True
    assert not GroupMember.objects.filter(extension__number="4242", logged_in=True).exists()
    # unknown code / unknown caller / not a member / wrong group -> not handled
    assert services.handle_feature_code(event, "4242", "*66", "4400") is False
    assert services.handle_feature_code(event, "1234", "*71", "4400") is False
    assert services.handle_feature_code(event, "4242", "*71", "4242") is False
    # self-service disabled -> not handled
    services.update_group(full_group, user, allow_self_service=False)
    assert services.handle_feature_code(event, "4300", "*72", "4400") is False


def test_feature_code_flag_off(full_group, event, settings):
    settings.DIAL_FEATURES = dict(settings.DIAL_FEATURES, callgroups=False)
    assert services.handle_feature_code(event, "4242", "*72", "4400") is False


def test_user_group_auto_join(event, user, other_user, orga, angels, member, ext_alice, ext_bob):
    from apps.events.models import EventMembership

    member.groups.add(angels)
    EventMembership.objects.create(event=event, user=other_user, role="user").groups.add(angels)
    g = services.create_group(event, orga, "4402", "Angels", "ringall", user_group=angels)
    assert sorted(g.members.values_list("extension__number", flat=True)) == ["4242", "4300"]
    assert GroupLoginLog.objects.filter(member__group=g, action="added", via="auto").count() == 2
    register(event, user, "4243", "dect")
    assert services.sync_user_group(g) == 1


def test_update_and_delete_group(group, user, ext_alice):
    services.update_group(group, user, name="Front Desk", strategy="longestidle", ring_timeout=15)
    group.extension.refresh_from_db()
    assert group.extension.display_name == "Front Desk" and group.extension.ring_timeout == 15
    with pytest.raises(CallGroupError):
        services.update_group(group, user, strategy="nope")
    services.add_member(group, ext_alice, user)
    services.delete_group(group, user)
    assert not CallGroup.objects.filter(pk=group.pk).exists()
    assert Extension.objects.get(number="4400").state == "deleted"


def test_export_event(full_group, event):
    data = export_event(event)["callgroups"]
    assert len(data) == 1 and data[0]["number"] == "4400" and len(data[0]["members"]) == 3
    assert data[0]["members"][0] == {"number": "4242", "logged_in": True, "priority": 0, "delay_s": 0}
    assert data[0]["shortcode"] == "" and data[0]["admins"] == []


def test_pbx_route_uses_dial_targets(client, full_group, event, user):
    """End-to-end: the PBX route endpoint asks our service for the group members."""
    from apps.pbx.api import hook_secret

    services.update_group(full_group, user, strategy="roundrobin")
    services.record_call(full_group, "4242")
    r = client.get("/api/v1/pbx/route/?event=demo&number=4400", HTTP_X_DIAL_PBX_SECRET=hook_secret())
    d = r.json()
    assert d["type"] == "group" and d["strategy"] == "serial"
    assert d["targets"] == ["Local/4300@dial-demo", "Local/4301@dial-demo", "Local/4242@dial-demo"]
