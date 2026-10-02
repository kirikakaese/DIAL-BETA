"""Dial-to-claim: pool handsets, adoption from the OMM, claiming via the PBX hook (dummy backends)."""
import pytest
from django.test import Client

from apps.core.models import AuditLog
from apps.dect import claim, services
from apps.dect.reverse import dect_reverse
from apps.devices.models import Device, DeviceBinding
from apps.extensions import services as ext_services
from apps.extensions.services import get_plan
from apps.pbx import dialplan as dp

pytestmark = pytest.mark.django_db

SECRET = "hook-secret-123"
HDR = {"HTTP_X_PET_PBX_SECRET": SECRET}


@pytest.fixture
def plan(event):
    p = get_plan(event)
    p.dect_claim_number = "9004"
    p.save()
    return p


@pytest.fixture
def pool_device(dect, event, plan):
    return claim.add_pool_handset(event, "0123456789099", name="pool #1")


# --------------------------------------------------------------------------- codes / dial string

def test_register_mints_claim_code_and_dial_string(dect, event, user, member, plan):
    ext = ext_services.register(event, user, "4242", "dect")
    assert ext.dect_claim_code and len(ext.dect_claim_code) == 6 and ext.dect_claim_code.isdigit()
    assert claim.claim_dial_string(plan, ext) == f"9004{ext.dect_claim_code}"
    sip = ext_services.register(event, user, "4243", "sip")
    assert sip.dect_claim_code == "" and claim.claim_dial_string(plan, sip) == ""


def test_dial_string_empty_when_feature_off(dect, event, user, member):
    ext = ext_services.register(event, user, "4242", "dect")
    assert claim.claim_dial_string(get_plan(event), ext) == ""


def test_legacy_extension_gets_code_lazily(dect, event, user, member, plan):
    ext = ext_services.register(event, user, "4242", "dect")
    ext.dect_claim_code = ""
    ext.save()
    assert claim.claim_dial_string(plan, ext).startswith("9004")
    ext.refresh_from_db()
    assert ext.dect_claim_code


def test_claim_number_is_reserved_and_produces_dialplan_rows(event, plan):
    assert "9004" in plan.service_numbers()
    assert not plan.evaluate("9004").allowed
    rows = dp.rows_for_plan(event, plan)
    by_exten = {}
    for r in rows:
        by_exten.setdefault(r.exten, []).append(r)
    assert by_exten["9004"][-1].appdata == "pet-services,dect-claim,1"
    suffix = by_exten["_9004X."]
    assert suffix[1].app == "Set" and suffix[1].appdata == "PET_CLAIM_CODE=${EXTEN:4}"
    assert suffix[2].appdata == "pet-services,dect-claim,1"
    assert {"9004", "_9004X."} <= dp.plan_extens(plan)


# --------------------------------------------------------------------------- pool + adoption

def test_add_pool_handset_provisions_temp_number(dect, event, plan, pool_device):
    d = pool_device
    assert d.unclaimed and d.owner is None and d.config["temp_number"] == "9004001"
    assert d.omm_ppn in dect.subs and dect.subs[d.omm_ppn]["number"] == "9004001"
    assert dect.subs[d.omm_ppn]["display_name"] == "Dial 9004+code"
    assert d.sip_username and d.subscription_pin and d.state == Device.State.PENDING
    with pytest.raises(ValueError):
        claim.add_pool_handset(event, "0123456789099")
    with pytest.raises(ValueError):
        claim.add_pool_handset(event, "123")
    second = claim.add_pool_handset(event, "0123456789098")
    assert second.config["temp_number"] == "9004002"


def test_sync_adopts_unknown_handsets_when_feature_on(dect, event, plan):
    ppn = dect.add_foreign_handset("0123456789077", number="777", rfp_id="3")
    summary = services.sync_infrastructure(event)
    assert summary["adopted"] == 1
    d = Device.objects.get(event=event, ipei="0123456789077")
    assert d.unclaimed and d.omm_ppn == ppn and d.state == Device.State.SUBSCRIBED
    assert d.config["temp_number"] == "777" and d.config["adopted"] is True
    # PET's SIP identity was pushed to the OMM user record
    assert dect.subs[ppn]["sip_user"] == d.sip_username and dect.subs[ppn]["sip_password"] == d.sip_password
    assert d.last_seen_rfp is not None and d.last_seen_rfp.omm_id == "3"
    # idempotent
    assert services.sync_infrastructure(event)["adopted"] == 0
    assert Device.objects.filter(event=event, ipei="0123456789077").count() == 1


def test_sync_adopts_handset_without_user_record(dect, event, plan):
    ppn = dect.add_foreign_handset("0123456789066", with_user=False)
    services.sync_infrastructure(event)
    d = Device.objects.get(event=event, ipei="0123456789066")
    assert d.omm_user_id == ppn and dect.subs[ppn]["sip_user"] == d.sip_username
    assert dect.subs[ppn]["number"] == "9004001"


def test_sync_ignores_unknown_handsets_when_feature_off(dect, event):
    dect.add_foreign_handset("0123456789077")
    summary = services.sync_infrastructure(event)
    assert summary["adopted"] == 0 and not Device.objects.filter(ipei="0123456789077").exists()


# --------------------------------------------------------------------------- claiming

def test_claim_binds_pool_handset_and_renumbers(dect, event, user, member, plan, pool_device):
    ext = ext_services.register(event, user, "4242", "dect")
    res = claim.claim_handset(event, pool_device.sip_username, ext.dect_claim_code)
    assert res.ok and res.code == "claimed" and res.number == "4242"
    pool_device.refresh_from_db()
    assert not pool_device.unclaimed and pool_device.owner == user and "temp_number" not in pool_device.config
    assert DeviceBinding.objects.filter(device=pool_device, extension=ext, is_active=True).exists()
    assert dect.subs[pool_device.omm_ppn]["number"] == "4242"
    assert dect.subs[pool_device.omm_ppn]["display_name"] == ext.caller_id_name
    assert AuditLog.objects.filter(action="update", message__icontains="claimed").exists()
    # idempotent: dialling again is fine and does not duplicate bindings
    assert claim.claim_handset(event, pool_device.sip_username, ext.dect_claim_code).ok
    assert DeviceBinding.objects.filter(device=pool_device).count() == 1


def test_claim_resolves_caller_by_temp_number_and_bound_number(dect, event, user, member, plan, pool_device):
    ext = ext_services.register(event, user, "4242", "dect")
    assert claim.resolve_caller(event, "9004001") == pool_device
    assert claim.resolve_caller(event, "", pool_device.sip_username) == pool_device
    claim.claim_handset(event, "9004001", ext.dect_claim_code)
    assert claim.resolve_caller(event, "4242") == pool_device
    assert claim.resolve_caller(event, "nope", "") is None


def test_claim_moves_handset_between_extensions(dect, event, user, other_user, member, plan, pool_device):
    from apps.events.models import EventMembership

    EventMembership.objects.create(event=event, user=other_user, role="user")
    a = ext_services.register(event, user, "4242", "dect")
    b = ext_services.register(event, other_user, "4343", "dect")
    claim.claim_handset(event, pool_device.sip_username, a.dect_claim_code)
    res = claim.claim_handset(event, pool_device.sip_username, b.dect_claim_code)
    assert res.ok and res.number == "4343"
    pool_device.refresh_from_db()
    assert pool_device.owner == other_user
    numbers = DeviceBinding.objects.filter(device=pool_device).values_list("extension__number", flat=True)
    assert list(numbers) == ["4343"]
    assert dect.subs[pool_device.omm_ppn]["number"] == "4343"
    assert a.bindings.count() == 0


def test_claim_errors(dect, event, user, member, plan, pool_device):
    ext = ext_services.register(event, user, "4242", "dect")
    sip_ext = ext_services.register(event, user, "4243", "sip")
    sip_ext.issue_dect_claim_code()
    assert claim.claim_handset(event, "unknown-endpoint", ext.dect_claim_code).code == "unknown_handset"
    assert claim.claim_handset(event, pool_device.sip_username, "000000x").code == "unknown_code"
    assert claim.claim_handset(event, pool_device.sip_username, "").code == "unknown_code"
    assert claim.claim_handset(event, pool_device.sip_username, sip_ext.dect_claim_code).code == "not_dect"
    ext.state = "suspended"
    ext.save()
    assert claim.claim_handset(event, pool_device.sip_username, ext.dect_claim_code).code == "unknown_code"
    pool_device.refresh_from_db()
    assert pool_device.unclaimed
    plan.dect_claim_number = ""
    plan.save()
    assert claim.claim_handset(event, pool_device.sip_username, ext.dect_claim_code).code == "feature_off"


def test_claim_survives_omm_failure(dect, event, user, member, plan, pool_device, monkeypatch):
    ext = ext_services.register(event, user, "4242", "dect")

    def boom(**kw):
        raise RuntimeError("OMM down")

    monkeypatch.setattr(dect, "update_subscription", boom)
    res = claim.claim_handset(event, pool_device.sip_username, ext.dect_claim_code)
    assert res.ok and "OMM down" in res.message
    pool_device.refresh_from_db()
    assert not pool_device.unclaimed and "OMM down" in pool_device.config["provision_error"]


def test_new_claim_code_invalidates_old(dect, event, user, member, plan, pool_device):
    ext = ext_services.register(event, user, "4242", "dect")
    old = ext.dect_claim_code
    ext.issue_dect_claim_code()
    assert ext.dect_claim_code != old
    assert claim.claim_handset(event, pool_device.sip_username, old).code == "unknown_code"
    assert claim.claim_handset(event, pool_device.sip_username, ext.dect_claim_code).ok


# --------------------------------------------------------------------------- PBX hook

def test_hook_dect_claim(client, dect, event, user, member, plan, pool_device, settings):
    settings.PET_PBX_HOOK_SECRET = SECRET
    ext = ext_services.register(event, user, "4242", "dect")
    r = client.post("/api/v1/pbx/hooks/dect-claim/",
                    {"event": "demo", "caller": pool_device.sip_username, "callerid": "9004001",
                     "code": ext.dect_claim_code}, **HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["handled"] is True and body["result"] == "claimed" and body["number"] == body["say"] == "4242"
    r = client.post("/api/v1/pbx/hooks/dect-claim/", {"event": "demo", "caller": "ghost", "code": "1"}, **HDR)
    assert r.json()["handled"] is False and r.json()["result"] == "unknown_handset"


# --------------------------------------------------------------------------- portal / orga pages

@pytest.mark.urls("apps.dect.tests.urls_stub")
def test_orga_pool_add_and_remove(dect, event, orga, plan):
    c = Client()
    c.force_login(orga)
    url = dect_reverse("handsets", kwargs={"slug": "demo"})
    r = c.post(dect_reverse("pool_add", kwargs={"slug": "demo"}), {"ipei": "0123 4567 8909 9", "name": "pool #1"},
               follow=True)
    assert r.status_code == 200
    d = Device.objects.get(event=event, ipei="0123456789099")
    assert d.unclaimed and "9004001" in r.content.decode() and "pool #1" in r.content.decode()
    r = c.post(dect_reverse("pool_add", kwargs={"slug": "demo"}), {"ipei": "12"}, follow=True)
    assert "13 digits" in r.content.decode()
    r = c.post(dect_reverse("pool_remove", kwargs={"slug": "demo", "pk": d.pk}), follow=True)
    assert r.status_code == 200 and not Device.objects.filter(pk=d.pk).exists() and d.omm_ppn not in dect.subs
    assert "Claim pool" in c.get(url).content.decode()


def test_extension_page_shows_claim_dial_and_regenerates(client, dect, event, user, member, plan):
    ext = ext_services.register(event, user, "4242", "dect")
    client.force_login(user)
    url = f"/e/{event.slug}/extensions/{ext.pk}/"
    html = client.get(url).content.decode()
    assert f"9004{ext.dect_claim_code}" in html and "Claim a handset by dialing" in html
    old = ext.dect_claim_code
    r = client.post(f"/e/{event.slug}/extensions/{ext.pk}/claim-code/", follow=True)
    assert r.status_code == 200
    ext.refresh_from_db()
    assert ext.dect_claim_code != old and f"9004{ext.dect_claim_code}" in r.content.decode()
    # feature off → classic instructions only
    plan.dect_claim_number = ""
    plan.save()
    html = client.get(url).content.decode()
    assert "Claim a handset by dialing" not in html and "How to subscribe a DECT handset" in html
