import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.extensions.models import Extension
from apps.ivr import services
from apps.ivr.export import export_event

pytestmark = pytest.mark.django_db


def test_create_announcement_registers_extension(event, user, member):
    ann = services.create_announcement(event, user, "4801", "Welcome to Demo Camp")
    ext = Extension.objects.get(number="4801", event=event)
    assert ext.type == "announcement" and ext.state == "active" and ann.extension == ext
    d = services.dialplan_for(ext)
    assert d["type"] == "announcement" and d["greeting"] == "tts:Welcome to Demo Camp" and d["options"] == {}
    assert ext.config["audio"] == "tts:Welcome to Demo Camp"


def test_announcement_audio_greeting(event, user, member):
    audio = SimpleUploadedFile("hello.wav", b"RIFF....", content_type="audio/wav")
    ann = services.create_announcement(event, user, "4802", audio=audio, loop=True)
    d = services.dialplan_for(ann.extension)
    assert d["loop"] is True and "hello" in d["greeting"]


def test_create_menu_and_dialplan(event, user, member):
    opts = [{"digit": "1", "action": "dial", "target": "4242", "label": "Bar"},
            {"digit": "2", "action": "voicemail", "target": "4300"},
            {"digit": "0", "action": "hangup"}]
    menu = services.create_menu(event, user, "4800", opts, prompt_tts="Press 1 for the bar", timeout=7)
    assert menu.extension.type == "ivr"
    d = services.dialplan_for(menu.extension)
    assert d["type"] == "ivr" and d["timeout"] == 7 and d["retries"] == 3
    assert d["options"] == {"1": "4242", "2": "vm:4300", "0": "hangup"}
    assert export_event(event)["ivr"]["menus"][0]["number"] == "4800"


def test_validate_options():
    with pytest.raises(services.IVRError):
        services.validate_options([{"digit": "x", "action": "dial", "target": "1"}])
    with pytest.raises(services.IVRError):
        services.validate_options([{"digit": "1", "action": "dial", "target": "1"}, {"digit": "1", "action": "hangup"}])
    with pytest.raises(services.IVRError):
        services.validate_options([{"digit": "1", "action": "teleport", "target": "1"}])
    with pytest.raises(services.IVRError):
        services.validate_options([{"digit": "1", "action": "dial"}])
    assert services.validate_options([{"digit": 1, "action": "hangup"}]) == [
        {"digit": "1", "action": "hangup", "target": "", "label": ""}]


def test_flag_off_returns_empty(event, user, member, settings):
    ann = services.create_announcement(event, user, "4801", "hi")
    settings.PET_FEATURES = {**settings.PET_FEATURES, "ivr": False}
    assert services.dialplan_for(ann.extension) == {}
    with pytest.raises(services.IVRError):
        services.create_announcement(event, user, "4803", "hi")


def test_dialplan_for_unrelated_extension(event, user, member):
    from apps.extensions.services import register

    assert services.dialplan_for(register(event, user, "4242", "dect")) == {}
    assert services.dialplan_for(None) == {}


def test_tts_render_without_command_is_none(settings):
    settings.PET_TTS_COMMAND = None
    assert services.tts_render("hello") is None


def test_pbx_route_uses_dialplan(client, event, user, member, settings):
    services.create_menu(event, user, "4800", [{"digit": "1", "action": "dial", "target": "4242"}],
                         prompt_tts="menu")
    settings.PET_PBX_HOOK_SECRET = "s3cret"
    d = client.get("/api/v1/pbx/route/?event=demo&number=4800", HTTP_X_PET_PBX_SECRET="s3cret").json()
    assert d["type"] == "ivr" and d["ivr_greeting"] == "tts:menu" and d["ivr_options"] == {"1": "4242"}


def test_views(client, event, user, other_user, member):
    url = reverse("ivr:index", args=[event.slug])
    assert client.get(url).status_code == 302
    client.force_login(user)
    assert client.get(url).status_code == 200
    r = client.post(reverse("ivr:announcement_new", args=[event.slug]),
                    {"number": "4801", "tts_text": "hello", "language": "en"})
    assert r.status_code == 302, r.content.decode()[:500]
    ann = Extension.objects.get(number="4801").announcement
    r = client.post(reverse("ivr:menu_new", args=[event.slug]), {
        "number": "4800", "prompt_tts": "menu", "language": "en", "timeout": 5, "invalid_retries": 3,
        "opt-TOTAL_FORMS": "2", "opt-INITIAL_FORMS": "0", "opt-MIN_NUM_FORMS": "0", "opt-MAX_NUM_FORMS": "1000",
        "opt-0-digit": "1", "opt-0-action": "dial", "opt-0-target": "4801", "opt-0-label": "Hello",
        "opt-1-digit": "2", "opt-1-action": "hangup", "opt-1-target": "", "opt-1-label": "",
    })
    assert r.status_code == 302, r.content.decode()[:500]
    menu = Extension.objects.get(number="4800").ivr_menu
    assert [o["digit"] for o in menu.options] == ["1", "2"]
    assert client.get(reverse("ivr:menu_edit", args=[event.slug, menu.pk])).status_code == 200
    client.force_login(other_user)
    assert client.get(reverse("ivr:announcement_edit", args=[event.slug, ann.pk])).status_code == 403


def test_api(client, event, user, member):
    client.force_login(user)
    r = client.post("/api/v1/ivr/announcements/", {"event": "demo", "number": "4801", "tts_text": "hi"})
    assert r.status_code == 201 and r.json()["extension_number"] == "4801"
    r = client.post("/api/v1/ivr/menus/", {"event": "demo", "number": "4800", "prompt_tts": "m",
                                            "options": [{"digit": "1", "action": "dial", "target": "4801"}]},
                    content_type="application/json")
    assert r.status_code == 201 and r.json()["dialplan"]["options"] == {"1": "4801"}
    r = client.post("/api/v1/ivr/menus/", {"event": "demo", "number": "4810",
                                            "options": [{"digit": "z", "action": "dial", "target": "1"}]},
                    content_type="application/json")
    assert r.status_code == 400
