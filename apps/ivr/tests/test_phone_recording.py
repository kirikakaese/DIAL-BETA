"""Recording announcements by phone: record code, start/finish hooks, identity rule, storage, webhook."""
import os
import wave

import pytest
from django.core.files.storage import default_storage
from rest_framework.test import APIClient

from apps.core.models import AuditLog
from apps.devices.models import Device, DeviceBinding
from apps.events.models import EventMembership
from apps.extensions import services as ext_services
from apps.extensions.models import Extension
from apps.extensions.services import get_plan
from apps.ivr import services

pytestmark = pytest.mark.django_db

SECRET = "hook-secret-123"
HDR = {"HTTP_X_PET_PBX_SECRET": SECRET}


@pytest.fixture(autouse=True)
def _dirs(settings, tmp_path):
    settings.PET_PBX_HOOK_SECRET = SECRET
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PET_RECORDING_DIR = str(tmp_path / "rec")
    os.makedirs(settings.PET_RECORDING_DIR)


@pytest.fixture
def plan(event):
    p = get_plan(event)
    p.announcement_record_number = "9005"
    p.save()
    return p


@pytest.fixture
def ann(event, user, member, plan):
    return services.create_announcement(event, user, "4801", "Welcome")


@pytest.fixture
def alice_phone(event, user, member):
    """Alice's DECT extension 4242 with a bound handset (PJSIP endpoint demo-alice)."""
    ext = ext_services.register(event, user, "4242", "dect")
    dev = Device.objects.create(event=event, owner=user, type="dect", sip_username="demo-alice",
                                state=Device.State.SUBSCRIBED)
    DeviceBinding.objects.create(extension=ext, device=dev)
    return ext, dev


@pytest.fixture
def bob_phone(event, other_user):
    EventMembership.objects.create(event=event, user=other_user, role="user")
    ext = ext_services.register(event, other_user, "4300", "sip")
    dev = Device.objects.create(event=event, owner=other_user, type="sip", sip_username="demo-bob",
                                state=Device.State.SUBSCRIBED)
    DeviceBinding.objects.create(extension=ext, device=dev)
    return ext, dev


def make_wav(path, seconds=2):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000 * seconds)
    return path


# --------------------------------------------------------------------------- code issuing

def test_record_code_issued_on_registration_and_dial_string(ann, plan):
    ext = ann.extension
    assert ext.announcement_record_code and len(ext.announcement_record_code) == 6
    assert ext.announcement_record_code.isdigit()
    assert services.record_dial_string(plan, ext) == f"9005{ext.announcement_record_code}"
    # feature off / other types -> ""
    plan.announcement_record_number = ""
    assert services.record_dial_string(plan, ext) == ""


def test_legacy_announcement_gets_code_lazily(ann, plan):
    ext = ann.extension
    ext.announcement_record_code = ""
    ext.save()
    assert services.record_dial_string(plan, ext).startswith("9005")
    ext.refresh_from_db()
    assert ext.announcement_record_code


def test_new_code_invalidates_old(event, ann, alice_phone):
    ext = ann.extension
    old = ext.announcement_record_code
    ext.issue_record_code()
    assert ext.announcement_record_code != old
    assert services.begin_phone_recording(event, "demo-alice", old, callerid="4242") is None
    assert services.begin_phone_recording(event, "demo-alice", ext.announcement_record_code, callerid="4242")


# --------------------------------------------------------------------------- begin

def test_begin_returns_payload(event, ann, alice_phone, settings):
    res = services.begin_phone_recording(event, "demo-alice", ann.extension.announcement_record_code,
                                         callerid="4242")
    assert res["handled"] is True and res["number"] == "4801"
    assert res["file"].startswith(settings.PET_RECORDING_DIR + os.sep) and "demo-4801-" in res["name"]
    assert res["file"] == os.path.join(settings.PET_RECORDING_DIR, res["name"])


def test_begin_wrong_code_or_feature_off(event, ann, alice_phone, plan):
    assert services.begin_phone_recording(event, "demo-alice", "000000", callerid="4242") is None
    assert services.begin_phone_recording(event, "demo-alice", "", callerid="4242") is None
    ann.extension.state = "suspended"
    ann.extension.save()
    assert services.begin_phone_recording(event, "demo-alice", ann.extension.announcement_record_code,
                                          callerid="4242") is None
    ann.extension.state = "active"
    ann.extension.save()
    plan.announcement_record_number = ""
    plan.save()
    assert services.begin_phone_recording(event, "demo-alice", ann.extension.announcement_record_code,
                                          callerid="4242") is None


def test_identity_rule(event, ann, alice_phone, bob_phone, other_user):
    code = ann.extension.announcement_record_code
    # owner via device, owner via caller id only, unknown caller
    assert services.begin_phone_recording(event, "demo-alice", code)
    assert services.begin_phone_recording(event, "", code, callerid="4242")
    assert services.begin_phone_recording(event, "nope", code, callerid="1111") is None
    # bob is a plain user -> refused; as helpdesk -> allowed
    assert services.begin_phone_recording(event, "demo-bob", code, callerid="4300") is None
    EventMembership.objects.filter(event=event, user=other_user).update(role="helpdesk")
    assert services.begin_phone_recording(event, "demo-bob", code, callerid="4300")
    # ownerless announcement: any active endpoint of the event may record
    EventMembership.objects.filter(event=event, user=other_user).update(role="user")
    Extension.objects.filter(pk=ann.extension.pk).update(owner=None)
    assert services.begin_phone_recording(event, "demo-bob", code, callerid="4300")
    assert services.begin_phone_recording(event, "nope", code, callerid="1111") is None


# --------------------------------------------------------------------------- finish

def test_finish_imports_wav_and_emits(event, ann, alice_phone, settings, monkeypatch):
    seen = []
    monkeypatch.setattr(services, "emit", lambda t, p, event=None: seen.append((t, p)))
    provisioned = []
    monkeypatch.setattr("apps.extensions.tasks.provision_extension.delay", lambda pk: provisioned.append(pk))
    code = ann.extension.announcement_record_code
    res = services.begin_phone_recording(event, "demo-alice", code, callerid="4242")
    path = make_wav(res["file"] + ".wav", seconds=3)
    out = services.finish_phone_recording(event, code, path, 0)
    assert out is not None and out.pk == ann.pk
    ann.refresh_from_db()
    assert ann.audio and ann.audio.name.startswith("ivr/demo/4801/phone-")
    with open(path, "rb") as fh, default_storage.open(ann.audio.name, "rb") as stored:
        assert stored.read() == fh.read()
    assert ann.tts_text == "Welcome"  # kept as fallback, audio wins
    d = services.dialplan_for(ann.extension)
    assert d["greeting"].endswith(ann.audio.name)
    ext = Extension.objects.get(pk=ann.extension.pk)
    rec = ext.config["phone_recording"]
    assert rec["imported"] is True and rec["duration"] == 3 and ext.config["audio"] == d["greeting"]
    assert seen and seen[0][0] == "announcement.recorded"
    assert seen[0][1]["extension"] == "4801" and seen[0][1]["imported"] is True and seen[0][1]["duration"] == 3
    assert provisioned == [str(ext.pk)]
    assert AuditLog.objects.filter(action="update", target_id=str(ext.pk),
                                   message__icontains="recorded by phone").exists()


def test_finish_keeps_reference_when_file_not_local(event, ann, settings):
    code = ann.extension.announcement_record_code
    out = services.finish_phone_recording(event, code, "/var/spool/asterisk/pet-recordings/demo-4801-x.wav", 7)
    assert out is not None
    ann.refresh_from_db()
    assert not ann.audio
    ext = Extension.objects.get(pk=ann.extension.pk)
    rec = ext.config["phone_recording"]
    assert rec["imported"] is False and rec["duration"] == 7
    # the PBX plays the file from its own disk until an upload/import replaces it
    assert services.dialplan_for(ext)["greeting"] == "/var/spool/asterisk/pet-recordings/demo-4801-x"


def test_finish_refuses_files_outside_recording_dir(event, ann, tmp_path):
    code = ann.extension.announcement_record_code
    outside = make_wav(str(tmp_path / "outside.wav"))
    services.finish_phone_recording(event, code, outside, 1)
    ann.refresh_from_db()
    assert not ann.audio


def test_finish_wrong_code(event, ann):
    assert services.finish_phone_recording(event, "000000", "/tmp/x.wav", 1) is None
    assert services.finish_phone_recording(event, "", "/tmp/x.wav", 1) is None


# --------------------------------------------------------------------------- hooks (HTTP)

def test_hooks_end_to_end(event, ann, alice_phone, settings):
    client = APIClient()
    code = ann.extension.announcement_record_code
    r = client.post("/api/v1/pbx/hooks/announcement-record-start/",
                    {"event": "demo", "caller": "demo-alice", "callerid": "4242", "code": code}, **HDR)
    assert r.status_code == 200
    d = r.json()
    assert d["handled"] is True and d["number"] == "4801" and d["file"] and d["name"]
    path = make_wav(d["file"] + ".wav")
    r = client.post("/api/v1/pbx/hooks/announcement-recorded/",
                    {"event": "demo", "code": code, "file": path, "duration": "2"}, **HDR)
    assert r.status_code == 200 and r.json() == {"handled": True, "number": "4801"}
    ann.refresh_from_db()
    assert ann.audio
    # wrong code -> handled false on both hooks
    r = client.post("/api/v1/pbx/hooks/announcement-record-start/",
                    {"event": "demo", "caller": "demo-alice", "callerid": "4242", "code": "999999"}, **HDR)
    assert r.json() == {"handled": False}
    r = client.post("/api/v1/pbx/hooks/announcement-recorded/",
                    {"event": "demo", "code": "999999", "file": path, "duration": "2"}, **HDR)
    assert r.json()["handled"] is False


def test_route_api_uses_recorded_audio(event, ann, alice_phone, settings):
    client = APIClient()
    code = ann.extension.announcement_record_code
    res = services.begin_phone_recording(event, "demo-alice", code, callerid="4242")
    services.finish_phone_recording(event, code, make_wav(res["file"] + ".wav"), 2)
    ann.refresh_from_db()
    d = client.get("/api/v1/pbx/route/?event=demo&number=4801", **HDR).json()
    assert d["type"] == "announcement" and d["ivr_greeting"].endswith(ann.audio.name)


# --------------------------------------------------------------------------- UI

def test_announcement_page_shows_code_and_new_code_button(client, event, user, ann, plan):
    client.force_login(user)
    url = f"/e/{event.slug}/ivr/announcement/{ann.pk}/edit/"
    html = client.get(url).content.decode()
    code = ann.extension.announcement_record_code
    assert f"9005{code}" in html and "Record by phone" in html and "New recording code" in html
    r = client.post(f"/e/{event.slug}/ivr/announcement/{ann.pk}/record-code/", follow=True)
    assert r.status_code == 200
    ann.extension.refresh_from_db()
    assert ann.extension.announcement_record_code != code
    assert f"9005{ann.extension.announcement_record_code}" in r.content.decode()
    # feature off -> no box
    plan.announcement_record_number = ""
    plan.save()
    assert "Record by phone" not in client.get(url).content.decode()


def test_new_code_forbidden_for_strangers(client, event, other_user, ann):
    EventMembership.objects.create(event=event, user=other_user, role="user")
    client.force_login(other_user)
    r = client.post(f"/e/{event.slug}/ivr/announcement/{ann.pk}/record-code/")
    assert r.status_code == 403
