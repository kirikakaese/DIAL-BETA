"""Portal view tests: self-service, devices, orga, helpdesk."""
import json

import pytest
from django.urls import reverse

from apps.core.models import AuditLog
from apps.devices.models import Device, DeviceBinding
from apps.events.models import Event, EventMembership, UserGroup
from apps.extensions import services
from apps.extensions.models import Extension, ExtensionTransfer
from apps.numbering.models import NumberRange

pytestmark = pytest.mark.django_db


def u(name, *args):
    return reverse(f"portal:{name}", args=args)


@pytest.fixture
def ext(event, user, member):
    return services.register(event, user, "4711", "dect", display_name="Alice")


# ------------------------------------------------------------------ public / dashboard

def test_home_anonymous_lists_events(client, event):
    r = client.get(u("home"))
    assert r.status_code == 200
    assert b"Demo Camp" in r.content


def test_home_redirects_logged_in(client, user):
    client.force_login(user)
    r = client.get(u("home"))
    assert r.status_code == 302 and r.url == u("dashboard")


def test_dashboard_groups_extensions_and_shows_joinable(client, user, event, other_user):
    Event.objects.create(name="Other", slug="other", state="live", start_date="2030-01-01", end_date="2030-01-02")
    services.register(event, user, "4712", "sip")
    client.force_login(user)
    r = client.get(u("dashboard"))
    assert r.status_code == 200
    assert b"4712" in r.content
    assert b"Other" in r.content  # joinable


def test_event_list_and_join(client, user, event):
    client.force_login(user)
    assert client.get(u("event_list")).status_code == 200
    r = client.post(u("event_join", event.slug), {"join_code": ""})
    assert r.status_code == 302
    assert EventMembership.objects.filter(event=event, user=user, role="user").exists()


def test_event_join_with_group_code(client, user, event, angels):
    angels.join_code = "secret"
    angels.save()
    client.force_login(user)
    client.post(u("event_join", event.slug), {"join_code": "secret"})
    assert "angels" in user.groups_for(event)


def test_event_dashboard_200(client, user, member, event):
    client.force_login(user)
    r = client.get(u("event_dashboard", event.slug))
    assert r.status_code == 200
    assert b"9000" in r.content  # test ringback in dialing help
    assert client.get(u("switch_event", event.slug)).status_code == 302


def test_event_create_superuser_only(client, user, admin):
    client.force_login(user)
    assert client.get(u("event_create")).status_code == 403
    client.force_login(admin)
    r = client.post(u("event_create"), {"name": "New", "slug": "new", "start_date": "2030-05-01",
                                        "end_date": "2030-05-03", "timezone": "Europe/Berlin", "is_public": "on"})
    assert r.status_code == 302
    ev = Event.objects.get(slug="new")
    assert ev.number_plan and ev.memberships.filter(user=admin, role="admin").exists()


# ------------------------------------------------------------------ extensions

def test_extension_create_instant(client, user, member, event):
    client.force_login(user)
    r = client.get(u("extension_create", event.slug) + "?number=4711")
    assert r.status_code == 200 and b"data-availability-url" in r.content
    r = client.post(u("extension_create", event.slug), {"number": "4711", "type": "dect", "in_phonebook": "on"})
    ext = Extension.objects.get(event=event, number="4711")
    assert ext.state == "active" and ext.owner == user
    assert r.url == u("extension_detail", event.slug, ext.pk)


def test_extension_create_requires_approval(client, user, member, event):
    client.force_login(user)
    client.post(u("extension_create", event.slug), {"number": "5555", "type": "dect"})  # vanity
    assert Extension.objects.get(event=event, number="5555").state == "requested"


def test_extension_create_taken_shows_error_and_waitlist(client, user, other_user, member, event):
    services.register(event, other_user, "4711", "dect")
    client.force_login(user)
    r = client.post(u("extension_create", event.slug), {"number": "4711", "type": "dect"})
    assert r.status_code == 200
    assert b"already taken" in r.content
    assert reverse("portal:waitlist_join", args=[event.slug]).encode() in r.content
    r = client.post(u("waitlist_join", event.slug), {"number": "4711", "type": "dect"})
    assert r.status_code == 302
    assert event.waitlist.filter(user=user, number="4711").exists()


def test_extension_detail_owner_ok_stranger_403_helpdesk_ok(client, user, other_user, ext, event):
    client.force_login(user)
    r = client.get(u("extension_detail", event.slug, ext.pk))
    assert r.status_code == 200 and b"4711" in r.content
    client.force_login(other_user)
    assert client.get(u("extension_detail", event.slug, ext.pk)).status_code == 403
    EventMembership.objects.create(event=event, user=other_user, role="helpdesk")
    assert client.get(u("extension_detail", event.slug, ext.pk)).status_code == 200
    # helpdesk may not edit
    assert client.get(u("extension_edit", event.slug, ext.pk)).status_code == 403


def test_extension_edit_and_delete(client, user, ext, event):
    client.force_login(user)
    r = client.post(u("extension_edit", event.slug, ext.pk), {
        "display_name": "Alice B", "description": "", "location_hint": "Tent 3", "in_phonebook": "on",
        "ring_strategy": "serial", "ring_timeout": 20, "forward_unconditional": "", "forward_busy": "4712",
        "forward_noanswer": "", "allow_callback": "on", "language": ""})
    assert r.status_code == 302
    ext.refresh_from_db()
    assert ext.display_name == "Alice B" and ext.ring_strategy == "serial" and ext.forward_busy == "4712"
    assert client.get(u("extension_delete", event.slug, ext.pk)).status_code == 200
    client.post(u("extension_delete", event.slug, ext.pk))
    ext.refresh_from_db()
    assert ext.state == "deleted"


def test_extension_port(client, user, member, event):
    old = Event.objects.create(name="Old", slug="old", state="archived", start_date="2020-01-01",
                               end_date="2020-01-03")
    src = Extension.objects.create(event=old, owner=user, number="4321", type="sip", state="active")
    client.force_login(user)
    r = client.get(u("extension_port", event.slug))
    assert b"4321" in r.content
    r = client.post(u("extension_port", event.slug), {"ext": [str(src.pk)]})
    assert r.status_code == 200
    assert Extension.objects.filter(event=event, number="4321", owner=user, ported_from=src).exists()


def test_transfer_flow(client, user, other_user, ext, event):
    client.force_login(user)
    r = client.post(u("extension_transfer", event.slug, ext.pk), {"recipient": "bob"})
    assert r.status_code == 302
    tr = ExtensionTransfer.objects.get(extension=ext)
    r = client.get(u("extension_transfer", event.slug, ext.pk))
    assert tr.token.encode() in r.content
    client.force_login(other_user)
    assert client.get(u("accept_transfer", tr.token)).status_code == 200
    r = client.post(u("accept_transfer", tr.token))
    assert r.status_code == 302
    ext.refresh_from_db()
    assert ext.owner == other_user


# ------------------------------------------------------------------ devices

def test_device_add_dect_creates_device_binding_and_pin(client, user, ext, event):
    client.force_login(user)
    assert client.get(u("device_add", event.slug, ext.pk)).status_code == 200
    r = client.post(u("device_add", event.slug, ext.pk), {
        "endpoint_type": "dect", "ipei": "0123456789012", "handset_model": "Gigaset", "name": "orange"})
    assert r.status_code == 302, r.content
    dev = Device.objects.get(event=event, ipei="0123456789012")
    assert dev.owner == user and dev.type == "dect" and dev.sip_username
    assert dev.subscription_pin and dev.pin_valid and dev.state == "pending"
    assert DeviceBinding.objects.filter(extension=ext, device=dev).exists()
    assert client.get(u("device_detail", event.slug, dev.pk)).status_code == 200
    # bad IPEI rejected
    r = client.post(u("device_add", event.slug, ext.pk), {"endpoint_type": "dect", "ipei": "123"})
    assert r.status_code == 200 and b"13 digits" in r.content


def test_device_sip_qr_rotate_and_unbind(client, user, member, event):
    ext = services.register(event, user, "4720", "sip")
    client.force_login(user)
    r = client.post(u("device_add", event.slug, ext.pk), {"endpoint_type": "sip", "name": "laptop",
                                                          "sip_transport": "tls", "mac_address": ""})
    assert r.status_code == 302
    dev = ext.bindings.get().device
    assert dev.sip_password and dev.sip_transport == "tls"
    r = client.get(u("device_qr", event.slug, dev.pk))
    assert r.status_code == 200 and r["Content-Type"] == "image/png" and r.content[:4] == b"\x89PNG"
    old_pw = dev.sip_password
    client.post(u("device_rotate_sip", event.slug, dev.pk))
    dev.refresh_from_db()
    assert dev.sip_password != old_pw
    client.post(u("device_unbind", event.slug, dev.pk, ext.pk))
    assert not Device.objects.filter(pk=dev.pk).exists()


def test_device_new_pin_by_helpdesk_is_audited(client, user, other_user, ext, event):
    dev = Device.objects.create(event=event, owner=user, type="dect", ipei="1111111111111")
    DeviceBinding.objects.create(extension=ext, device=dev)
    EventMembership.objects.create(event=event, user=other_user, role="helpdesk")
    client.force_login(other_user)
    r = client.post(u("device_new_pin", event.slug, dev.pk))
    assert r.status_code == 302
    dev.refresh_from_db()
    assert dev.pin_valid
    assert AuditLog.objects.filter(action="impersonate", actor=other_user).exists()


# ------------------------------------------------------------------ guests

def test_claim_guest_flow(client, user, orga, event):
    g = services.create_guest_extension(event, "6001", orga)
    client.force_login(user)
    r = client.get(u("claim_guest", g.claim_token))
    assert r.status_code == 200 and b"6001" in r.content
    r = client.post(u("claim_guest", g.claim_token))
    assert r.status_code == 302
    g.refresh_from_db()
    assert g.owner == user and g.state == "active"
    assert EventMembership.objects.filter(event=event, user=user).exists()


def test_orga_guests_create_and_print(client, orga, event):
    client.force_login(orga)
    r = client.post(u("orga_guests", event.slug), {"start": "6100", "count": 3, "numbers": ""})
    assert r.status_code == 200
    assert Extension.objects.filter(event=event, is_temporary=True).count() == 3
    r = client.get(u("orga_guests", event.slug) + "?print=1")
    assert r.status_code == 200 and b"data:image/png;base64," in r.content


# ------------------------------------------------------------------ orga

def test_orga_pages_403_for_user_200_for_orga(client, user, member, orga, event):
    pages = ["orga_dashboard", "orga_event_settings", "orga_numberplan", "orga_queue", "orga_extensions",
             "orga_members", "orga_groups", "orga_audit", "orga_webhooks", "orga_service_accounts",
             "orga_range_create"]
    client.force_login(user)
    for p in pages:
        assert client.get(u(p, event.slug)).status_code == 403, p
    client.force_login(orga)
    for p in pages:
        assert client.get(u(p, event.slug)).status_code == 200, p


def test_event_creation_and_cloning_are_admin_only(client, orga, admin, event):
    client.force_login(orga)
    assert client.get(reverse("portal:event_create")).status_code == 403
    assert client.get(u("orga_event_clone", event.slug)).status_code == 403
    r = client.post(u("orga_event_clone", event.slug), {"name": "Demo 2031", "slug": "demo31",
                                                        "start_date": "2031-08-01", "end_date": "2031-08-05"})
    assert r.status_code == 403 and not Event.objects.filter(slug="demo31").exists()
    client.force_login(admin)
    assert client.get(u("orga_event_clone", event.slug)).status_code == 200
    r = client.post(u("orga_event_clone", event.slug), {"name": "Demo 2031", "slug": "demo31",
                                                        "start_date": "2031-08-01", "end_date": "2031-08-05"})
    assert r.status_code == 302
    new = Event.objects.get(slug="demo31")
    assert new.state == "draft" and new.number_plan.ranges.count() == event.number_plan.ranges.count()
    assert new.memberships.filter(user=orga, role="orga").exists()  # orga team is carried over
    assert new.memberships.filter(user=admin, role="admin").exists()


def test_orga_queue_approve_and_reject(client, user, other_user, orga, event):
    a = services.register(event, user, "5555", "dect")
    b = services.register(event, other_user, "6666", "dect")
    client.force_login(orga)
    r = client.get(u("orga_queue", event.slug))
    assert b"5555" in r.content and b"6666" in r.content
    client.post(u("orga_moderate", event.slug, a.pk, "approve"), {"note": "ok"})
    client.post(u("orga_moderate", event.slug, b.pk, "reject"), {"note": "nope"})
    a.refresh_from_db(), b.refresh_from_db()
    assert a.state == "active" and a.moderation_note == "ok"
    assert b.state == "rejected"


def test_orga_extension_actions(client, user, orga, ext, event):
    client.force_login(orga)
    client.post(u("orga_extension_action", event.slug, ext.pk, "suspend"), {"note": "abuse"})
    ext.refresh_from_db()
    assert ext.state == "suspended"
    client.post(u("orga_extension_action", event.slug, ext.pk, "reactivate"))
    ext.refresh_from_db()
    assert ext.state == "active"
    r = client.get(u("orga_extensions", event.slug) + "?q=alice&state=active")
    assert b"4711" in r.content


def test_orga_numberplan_save_and_range_create(client, orga, event, angels):
    client.force_login(orga)
    plan = event.number_plan
    r = client.post(u("orga_numberplan", event.slug), {
        "min_length": 4, "max_length": 4, "default_allowed": "on", "test_ringback_number": "9000",
        "wakeup_service_number": "9001", "site_survey_number": "9002", "echo_test_number": "9003",
        "voicemail_number": "9999", "emergency_numbers": "112, 110", "callback_request_code": "*66",
        "callback_cancel_code": "*86", "group_login_code": "*71", "group_logout_code": "*72"})
    assert r.status_code == 302
    plan.refresh_from_db()
    assert plan.emergency_numbers == ["112", "110"]
    r = client.post(u("orga_range_create", event.slug), {
        "name": "Medics", "priority": 15, "is_active": "on", "prefix": "3", "pattern": "", "mode": "restricted",
        "allowed_roles": ["helpdesk"], "allowed_groups": [angels.pk], "allowed_types": ["dect", "sip"]})
    assert r.status_code == 302
    rng = NumberRange.objects.get(plan=plan, name="Medics")
    assert rng.allowed_roles == ["helpdesk"] and list(rng.allowed_groups.all()) == [angels]
    assert rng.allowed_types == ["dect", "sip"]
    # test-a-number mini form
    r = client.get(u("orga_numberplan", event.slug) + "?number=3001")
    assert r.status_code == 200 and b"Medics" in r.content
    assert client.get(u("orga_range_edit", event.slug, rng.pk)).status_code == 200
    r = client.post(u("orga_range_delete", event.slug, rng.pk))
    assert r.status_code == 302 and not NumberRange.objects.filter(pk=rng.pk).exists()


def test_orga_numberplan_saves_forward_codes_and_record_number(client, orga, event):
    client.force_login(orga)
    plan = event.number_plan
    html = client.get(u("orga_numberplan", event.slug)).content.decode()
    assert "Feature codes" in html and 'name="forward_set_code"' in html and 'name="announcement_record_number"' in html
    r = client.post(u("orga_numberplan", event.slug), {
        "min_length": 4, "max_length": 4, "default_allowed": "on", "test_ringback_number": "9000",
        "wakeup_service_number": "9001", "site_survey_number": "9002", "echo_test_number": "9003",
        "voicemail_number": "9999", "announcement_record_number": "9005", "emergency_numbers": "112, 110",
        "callback_request_code": "*66", "callback_cancel_code": "*86", "group_login_code": "*71",
        "group_logout_code": "*72", "forward_set_code": "*31", "forward_clear_code": "*30",
        "forward_busy_code": "", "forward_noanswer_code": "*33"})
    assert r.status_code == 302
    plan.refresh_from_db()
    assert plan.announcement_record_number == "9005"
    assert (plan.forward_set_code, plan.forward_clear_code, plan.forward_busy_code, plan.forward_noanswer_code) == (
        "*31", "*30", "", "*33")
    assert "9005" in plan.service_numbers()
    # the dialing help on the event dashboard lists the new codes
    html = client.get(u("event_dashboard", event.slug)).content.decode()
    assert "*31" in html and "*30" in html and "9005" in html


def test_orga_members_add_role_change_remove(client, orga, user, other_user, member, event, angels):
    client.force_login(orga)
    r = client.post(u("orga_members", event.slug), {"action": "add", "identifier": "bob@example.org",
                                                    "role": "helpdesk"})
    assert r.status_code == 302
    assert EventMembership.objects.get(event=event, user=other_user).role == "helpdesk"
    r = client.post(u("orga_members", event.slug), {"action": "update", "pk": member.pk, "role": "orga",
                                                    "groups": [angels.pk]})
    member.refresh_from_db()
    assert member.role == "orga" and list(member.groups.all()) == [angels]
    client.post(u("orga_members", event.slug), {"action": "remove", "pk": member.pk})
    assert not EventMembership.objects.filter(pk=member.pk).exists()


def test_orga_groups_crud(client, orga, event):
    client.force_login(orga)
    r = client.post(u("orga_groups", event.slug), {"name": "Medics", "slug": "medics", "description": "",
                                                   "join_code": "med1"})
    assert r.status_code == 302
    g = UserGroup.objects.get(event=event, slug="medics")
    client.post(u("orga_groups", event.slug), {"pk": g.pk, "name": "Medics!", "slug": "medics", "join_code": ""})
    g.refresh_from_db()
    assert g.name == "Medics!"
    client.post(u("orga_groups", event.slug), {"action": "delete", "pk": g.pk})
    assert not UserGroup.objects.filter(pk=g.pk).exists()


def test_orga_event_settings_and_state(client, orga, event):
    client.force_login(orga)
    r = client.post(u("orga_event_settings", event.slug), {
        "name": "Demo Camp 2", "description": "", "start_date": event.start_date, "end_date": event.end_date,
        "location": "Field", "timezone": "Europe/Berlin", "is_public": "on", "primary_color": "#112233",
        "accent_color": "#445566", "announcement": "Hi", "sip_domain": "demo.dial.local", "dial_prefix": "",
        "default_language": "de", "max_extensions_per_user": 5, "allow_guest_extensions": "on",
        "cdr_retention_days": "", "disabled_features": ["messaging"]})
    assert r.status_code == 302
    event.refresh_from_db()
    assert event.name == "Demo Camp 2" and event.settings["disabled_features"] == ["messaging"]
    r = client.post(u("orga_event_state", event.slug, "live"))
    event.refresh_from_db()
    assert event.state == "live"
    client.post(u("orga_event_state", event.slug, "draft"))  # invalid transition -> message, no crash
    event.refresh_from_db()
    assert event.state == "live"


def test_orga_export_json_and_import_superuser(client, orga, admin, event, ext):
    client.force_login(orga)
    r = client.get(u("orga_export", event.slug))
    assert r.status_code == 200 and r["Content-Type"] == "application/json"
    data = json.loads(r.content)
    assert data["event"]["slug"] == "demo" and any(e["number"] == "4711" for e in data["extensions"])
    assert client.get(u("orga_import", event.slug)).status_code == 403
    client.force_login(admin)
    assert client.get(u("orga_import", event.slug)).status_code == 200
    from django.core.files.uploadedfile import SimpleUploadedFile

    f = SimpleUploadedFile("x.json", r.content, content_type="application/json")
    r = client.post(u("orga_import", event.slug), {"file": f, "slug_override": "demo-copy"})
    assert r.status_code == 302
    assert Extension.objects.filter(event__slug="demo-copy", number="4711").exists()


def test_orga_webhooks_and_tokens(client, orga, event):
    client.force_login(orga)
    r = client.post(u("orga_webhooks", event.slug), {"name": "hook", "url": "https://example.org/h",
                                                     "secret": "s", "event_types": ["extension.created"],
                                                     "is_active": "on"})
    assert r.status_code == 302 and event.webhooks.get().event_types == ["extension.created"]
    r = client.post(u("orga_service_accounts", event.slug), {"name": "badge printer",
                                                             "scopes": "extensions:read, phonebook:read"})
    assert r.status_code == 200 and b"dial_" in r.content
    acct = event.service_accounts.get()
    assert acct.scopes == ["extensions:read", "phonebook:read"]
    client.post(u("orga_service_accounts", event.slug), {"action": "revoke", "pk": acct.pk})
    acct.refresh_from_db()
    assert not acct.is_active


def test_orga_audit_and_resync(client, orga, event, ext):
    client.force_login(orga)
    r = client.get(u("orga_audit", event.slug) + "?action=create&q=4711")
    assert r.status_code == 200 and b"4711" in r.content
    r = client.post(u("orga_resync", event.slug))
    assert r.status_code == 302
    assert AuditLog.objects.filter(event=event, action="provision").exists()


def test_helpdesk_lookup_permissions_and_results(client, user, other_user, orga, ext, event):
    Device.objects.create(event=event, owner=user, type="dect", ipei="2222222222222")
    client.force_login(user)
    assert client.get(u("helpdesk_lookup", event.slug)).status_code == 403
    client.force_login(orga)
    assert client.get(u("helpdesk_lookup", event.slug)).status_code == 200
    r = client.get(u("helpdesk_lookup", event.slug) + "?q=4711")
    assert b"alice" in r.content and b"2222222222222" in r.content
    r = client.get(u("helpdesk_lookup", event.slug) + "?q=22222")
    assert b"alice" in r.content
    EventMembership.objects.create(event=event, user=other_user, role="helpdesk")
    client.force_login(other_user)
    assert client.get(u("helpdesk_lookup", event.slug) + "?q=alice").status_code == 200
    assert client.get(u("orga_dashboard", event.slug)).status_code == 403
