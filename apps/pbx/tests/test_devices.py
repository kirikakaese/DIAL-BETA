"""Device sync writes ps_auths / ps_aors / ps_endpoints rows."""
import pytest

from apps.devices.models import Device
from apps.pbx.models import PBXSyncLog, PsAor, PsAuth, PsEndpoint

from .conftest import bind, make_device, make_extension

pytestmark = pytest.mark.django_db


def test_sync_sip_device_writes_ps_rows(pbx, event, user):
    ext = make_extension(event, "4242", owner=user, display_name="Alice")
    dev = make_device(event, "demo-aaaa", owner=user, transport="tcp")
    bind(ext, dev)
    pbx.sync_device(dev)

    auth = PsAuth.objects.get(id="demo-aaaa")
    assert auth.auth_type == "userpass" and auth.username == "demo-aaaa" and auth.password == "secretpw"
    aor = PsAor.objects.get(id="demo-aaaa")
    assert aor.max_contacts == 3 and aor.remove_existing == "yes"
    assert aor.mailboxes == "4242@pet-demo"
    ep = PsEndpoint.objects.get(id="demo-aaaa")
    assert ep.context == "pet-demo"
    assert ep.transport == "transport-tcp"
    assert ep.aors == "demo-aaaa" and ep.auth == "demo-aaaa"
    assert ep.callerid == '"4242 Alice" <4242>'  # display_mode default: number + name
    assert ep.set_var == "PET_EVENT=demo"
    assert ep.accountcode == "demo"
    assert ep.disallow == "all" and "alaw" in ep.allow
    assert ep.webrtc == "no"
    assert PBXSyncLog.objects.filter(kind="device", target="demo-aaaa").exists()


def test_dect_device_is_sip_endpoint_towards_omm(pbx, event, user):
    ext = make_extension(event, "4243", owner=user, display_name="Bob")
    dev = make_device(event, "demo-dect", dtype="dect", owner=user)
    bind(ext, dev)
    pbx.sync_device(dev)
    ep = PsEndpoint.objects.get(id="demo-dect")
    aor = PsAor.objects.get(id="demo-dect")
    assert aor.max_contacts == 1
    assert ep.device_state_busy_at == 1
    assert ep.callerid == '"4243 Bob" <4243>'


def test_device_without_extension_uses_username_callerid(pbx, event):
    dev = make_device(event, "demo-lone", name="spare phone")
    pbx.sync_device(dev)
    assert PsEndpoint.objects.get(id="demo-lone").callerid == '"spare phone" <demo-lone>'
    assert PsEndpoint.objects.get(id="demo-lone").mailboxes is None


def test_device_without_credentials_gets_them(pbx, event):
    dev = Device.objects.create(event=event, type="sip")
    pbx.sync_device(dev)
    dev.refresh_from_db()
    assert dev.sip_username and dev.sip_password
    assert PsAuth.objects.filter(id=dev.sip_username, password=dev.sip_password).exists()


def test_webrtc_device_flags(pbx, event):
    dev = make_device(event, "demo-web", dtype="webrtc", transport="wss")
    pbx.sync_device(dev)
    ep = PsEndpoint.objects.get(id="demo-web")
    assert ep.transport == "transport-wss"
    assert ep.webrtc == "yes" and ep.media_encryption == "dtls" and ep.ice_support == "yes"


def test_gsm_device_is_skipped(pbx, event):
    dev = Device.objects.create(event=event, type="gsm", msisdn="4711")
    pbx.sync_device(dev)
    assert PsEndpoint.objects.count() == 0


def test_sync_extension_syncs_bound_devices(pbx, ext_two_devices):
    pbx.sync_extension(ext_two_devices)
    assert set(PsEndpoint.objects.values_list("id", flat=True)) == {"demo-aaaa", "demo-bbbb"}


def test_remove_device(pbx, event):
    dev = make_device(event, "demo-gone")
    pbx.sync_device(dev)
    pbx.remove_device(dev)
    assert not PsEndpoint.objects.filter(id="demo-gone").exists()
    assert not PsAuth.objects.filter(id="demo-gone").exists()
    assert not PsAor.objects.filter(id="demo-gone").exists()


def test_sync_event_prunes_stale_endpoints(pbx, event, ext_two_devices):
    PsEndpoint.objects.create(id="demo-stale", accountcode="demo", context="pet-demo")
    PsAuth.objects.create(id="demo-stale")
    PsAor.objects.create(id="demo-stale")
    PsEndpoint.objects.create(id="other-keep", accountcode="other", context="pet-other")
    pbx.sync_event(event)
    ids = set(PsEndpoint.objects.values_list("id", flat=True))
    assert ids == {"demo-aaaa", "demo-bbbb", "other-keep"}
    assert not PsAuth.objects.filter(id="demo-stale").exists()
