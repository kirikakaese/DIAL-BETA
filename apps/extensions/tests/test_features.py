"""Per-extension features: ringback tone upload/processing, FK forwarding (validation, loop detection,
auto-clear), DECT encryption pass-through and the portal edit form."""
import io
import wave
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.models import AuditLog
from apps.extensions import audio, services
from apps.extensions.models import Extension, ExtensionType

pytestmark = pytest.mark.django_db


def make_wav(rate=44100, channels=2, width=2, seconds=0.05) -> bytes:
    n = int(rate * seconds)
    sample = bytes([200]) if width == 1 else (1000).to_bytes(width, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(sample * channels * n)
    return buf.getvalue()


def upload(name, data, size=None):
    f = SimpleUploadedFile(name, data, content_type="application/octet-stream")
    if size is not None:
        f.size = size
    return f


def make_ext(event, number, owner=None, state=Extension.State.ACTIVE, etype=ExtensionType.SIP, **fields):
    return Extension.objects.create(event=event, number=number, type=etype, owner=owner, state=state, **fields)


# --------------------------------------------------------------------------- upload validation

@pytest.mark.parametrize("head, fmt", [
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "wav"),
    (b"OggS\x00\x02", "ogg"),
    (b"fLaC\x00\x00", "flac"),
    (b"ID3\x04\x00", "mp3"),
    (b"\xff\xfb\x90\x00", "mp3"),
    (b"\xff\xf3\x90\x00", "mp3"),
    (b"\xff\xf2\x90\x00", "mp3"),
    (b"GIF89a", None),
    (b"", None),
])
def test_sniff_audio_format(head, fmt):
    assert audio.sniff_audio_format(head) == fmt


def test_validate_upload_accepts_matching_wav():
    assert audio.validate_ringback_upload(upload("ring.WAV", make_wav())) == "wav"


def test_validate_upload_rejects_too_large():
    with pytest.raises(ValidationError) as ei:
        audio.validate_ringback_upload(upload("ring.wav", make_wav(), size=audio.MAX_RINGBACK_BYTES + 1))
    assert ei.value.code == "too_large"


def test_validate_upload_rejects_bad_extension():
    with pytest.raises(ValidationError) as ei:
        audio.validate_ringback_upload(upload("ring.exe", make_wav()))
    assert ei.value.code == "bad_extension"


def test_validate_upload_rejects_bad_content():
    with pytest.raises(ValidationError) as ei:
        audio.validate_ringback_upload(upload("ring.mp3", b"GIF89a" + b"\x00" * 100))
    assert ei.value.code == "bad_content"


def test_validate_upload_rejects_extension_content_mismatch():
    with pytest.raises(ValidationError) as ei:
        audio.validate_ringback_upload(upload("ring.mp3", make_wav()))
    assert ei.value.code == "mismatch"


# --------------------------------------------------------------------------- conversion

def _wav_params(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


def test_wave_fallback_converts_to_8k_mono_16bit():
    with mock.patch.object(audio.shutil, "which", return_value=None):
        out = audio.convert_to_asterisk_wav(make_wav(rate=44100, channels=2), "ring.wav")
    channels, width, rate, frames = _wav_params(out)
    assert (channels, width, rate) == (1, 2, 8000)
    assert 380 <= frames <= 420  # 0.05 s at 8 kHz = 400 frames


def test_wave_fallback_handles_8bit_input():
    with mock.patch.object(audio.shutil, "which", return_value=None):
        out = audio.convert_to_asterisk_wav(make_wav(rate=8000, channels=1, width=1), "ring.wav")
    assert _wav_params(out)[:3] == (1, 2, 8000)


def test_non_wav_without_ffmpeg_fails_with_hint():
    with mock.patch.object(audio.shutil, "which", return_value=None):
        with pytest.raises(audio.RingbackError, match="ffmpeg is not installed"):
            audio.convert_to_asterisk_wav(b"ID3\x04\x00" + b"\x00" * 64, "ring.mp3")


def test_ffmpeg_path_is_used_when_available():
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        with open(cmd[-1], "wb") as fh:
            fh.write(make_wav(rate=8000, channels=1))
        return mock.Mock(returncode=0, stderr=b"")

    with mock.patch.object(audio.shutil, "which", return_value="/usr/bin/ffmpeg"), \
            mock.patch.object(audio.subprocess, "run", side_effect=fake_run):
        out = audio.convert_to_asterisk_wav(b"ID3\x04\x00" + b"\x00" * 64, "ring.mp3")
    assert calls and calls[0][0] == "/usr/bin/ffmpeg" and "-ar" in calls[0] and "8000" in calls[0]
    assert _wav_params(out)[:3] == (1, 2, 8000)


def test_ffmpeg_failure_is_reported():
    with mock.patch.object(audio.shutil, "which", return_value="/usr/bin/ffmpeg"), \
            mock.patch.object(audio.subprocess, "run",
                              return_value=mock.Mock(returncode=1, stderr=b"Invalid data found")):
        with pytest.raises(audio.RingbackError, match="Invalid data found"):
            audio.convert_to_asterisk_wav(make_wav(), "ring.wav")


# --------------------------------------------------------------------------- task + services

def test_set_ringback_tone_processes_and_marks_ready(event, user):
    ext = make_ext(event, "4242", owner=user)
    with mock.patch.object(audio.shutil, "which", return_value=None):
        services.set_ringback_tone(ext, upload("ring.wav", make_wav()), user)  # eager celery runs the task
    ext.refresh_from_db()
    assert ext.ringback_tone_status == Extension.RingbackStatus.READY
    assert ext.has_ringback_tone and ext.ringback_tone_processed.name == f"ringback/processed/{ext.pk}/tone.wav"
    assert _wav_params(ext.ringback_tone_processed.read())[:3] == (1, 2, 8000)
    assert ext.ringback_class == "dial-demo-4242"
    assert AuditLog.objects.filter(target_id=str(ext.pk), message="Ringback tone uploaded").exists()

    services.clear_ringback_tone(ext, user)
    ext.refresh_from_db()
    assert ext.ringback_tone_status == Extension.RingbackStatus.NONE
    assert not ext.ringback_tone and not ext.ringback_tone_processed


def test_task_marks_failed_with_error(event, user):
    ext = make_ext(event, "4242", owner=user)
    ext.ringback_tone.save("ring.mp3", io.BytesIO(b"ID3\x04\x00" + b"\x00" * 64), save=True)
    with mock.patch.object(audio.shutil, "which", return_value=None):
        assert audio.process_ringback_tone(str(ext.pk)) == "failed"
    ext.refresh_from_db()
    assert ext.ringback_tone_status == Extension.RingbackStatus.FAILED
    assert "ffmpeg" in ext.ringback_tone_error
    assert not ext.has_ringback_tone


def test_task_without_upload_resets_to_none(event, user):
    ext = make_ext(event, "4242", owner=user, ringback_tone_status=Extension.RingbackStatus.PENDING)
    audio.process_ringback_tone(str(ext.pk))
    ext.refresh_from_db()
    assert ext.ringback_tone_status == Extension.RingbackStatus.NONE
    assert audio.process_ringback_tone("00000000-0000-0000-0000-000000000000") is None


# --------------------------------------------------------------------------- forwarding

def test_set_forwarding_and_properties(event, user):
    a = make_ext(event, "4242", owner=user)
    b = make_ext(event, "4300", owner=user)
    services.set_forwarding(a, user, mode="delayed", target=b, delay=7)
    a.refresh_from_db()
    assert a.forward_mode == "delayed" and a.forward_target == b and a.forward_delay == 7
    assert a.forward_target_number == "4300" and a.is_forwarding
    assert b.is_forward_target and list(b.live_forwarders()) == [a]
    entry = AuditLog.objects.filter(target_id=str(a.pk), action="update").latest("created_at")
    assert entry.changes["forward_target"] == [None, "4300"] and entry.changes["forward_mode"] == ["off", "delayed"]


def test_forward_validation_rejects_self_other_event_and_dead_targets(event, user):
    import datetime as dt

    a = make_ext(event, "4242", owner=user)
    with pytest.raises(services.ExtensionError, match="itself"):
        services.validate_forward_target(a, a)
    other = event.clone(name="Other", slug="other", start_date=dt.date.today(), end_date=dt.date.today())
    foreign = make_ext(other, "4300", owner=user)
    with pytest.raises(services.ExtensionError, match="same event"):
        services.validate_forward_target(a, foreign)
    dead = make_ext(event, "4301", owner=user, state=Extension.State.DELETED)
    with pytest.raises(services.ExtensionError, match="not an active"):
        services.validate_forward_target(a, dead)
    with pytest.raises(services.ExtensionError, match="choose a forwarding target"):
        services.set_forwarding(a, user, mode="always", target=None)


def test_forward_loop_detection(event, user):
    a = make_ext(event, "4242", owner=user)
    b = make_ext(event, "4300", owner=user)
    c = make_ext(event, "4301", owner=user)
    services.set_forwarding(a, user, mode="always", target=b)
    services.set_forwarding(b, user, mode="busy", target=c)
    with pytest.raises(services.ExtensionError, match="loop"):
        services.set_forwarding(c, user, mode="noanswer", target=a)
    with pytest.raises(services.ExtensionError, match="loop"):
        services.set_forwarding(b, user, mode="always", target=a)
    # a chain that does not come back is fine
    d = make_ext(event, "4302", owner=user)
    services.set_forwarding(c, user, mode="always", target=d)
    c.refresh_from_db()
    assert c.forward_target == d


def test_forward_chain_too_long(event, user):
    exts = [make_ext(event, str(4300 + i), owner=user) for i in range(12)]
    for cur, nxt in zip(exts, exts[1:], strict=False):
        cur.forward_mode = "always"
        cur.forward_target = nxt
        cur.save()
    new = make_ext(event, "4242", owner=user)
    with pytest.raises(services.ExtensionError, match="too long"):
        services.validate_forward_target(new, exts[0])


def test_resolve_forward_target_prefers_active(event, user):
    make_ext(event, "4300", owner=user, state=Extension.State.DELETED)
    sus = make_ext(event, "4301", owner=user, state=Extension.State.SUSPENDED)
    assert services.resolve_forward_target(event, "4300") is None
    assert services.resolve_forward_target(event, "4301") == sus
    assert services.resolve_forward_target(event, "") is None


def test_delete_clears_forwards_and_reprovisions(event, user, other_user):
    from apps.pbx import get_pbx, reset_pbx_cache

    reset_pbx_cache()
    pbx = get_pbx()
    a = make_ext(event, "4242", owner=user)
    b = make_ext(event, "4300", owner=other_user)
    services.set_forwarding(a, user, mode="always", target=b)
    pbx.reset()
    services.delete(b, other_user)
    a.refresh_from_db()
    assert a.forward_mode == "off" and a.forward_target is None
    assert not b.is_forward_target
    entry = AuditLog.objects.filter(target_id=str(a.pk), message__startswith="Forwarding cleared").get()
    assert entry.actor == other_user and entry.changes["forward_target"] == ["4300", None]
    assert str(a.pk) in pbx.extensions  # re-provisioned via provision_extension
    reset_pbx_cache()


def test_expire_and_reject_clear_forwards_via_signal(event, user, orga):
    a = make_ext(event, "4242", owner=user)
    b = make_ext(event, "4300", owner=user)
    services.set_forwarding(a, user, mode="busy", target=b)
    b.mark_expired()
    a.refresh_from_db()
    assert a.forward_mode == "off" and a.forward_target is None

    c = make_ext(event, "4301", owner=user, state=Extension.State.REQUESTED)
    a.forward_mode, a.forward_target = "noanswer", c
    a.save()
    services.reject(c, orga)
    a.refresh_from_db()
    assert a.forward_mode == "off" and a.forward_target is None
    # saving an already-dead extension again does not log anything new
    n = AuditLog.objects.filter(message__startswith="Forwarding cleared").count()
    c.moderation_note = "x"
    c.save()
    assert AuditLog.objects.filter(message__startswith="Forwarding cleared").count() == n


# --------------------------------------------------------------------------- DECT encryption

def test_dect_encryption_reaches_dummy_backend(event, user):
    from apps.dect import get_dect, reset_dect_cache
    from apps.dect.provisioning import provision_dect_for_extension
    from apps.devices.models import Device, DeviceBinding
    from apps.pbx import reset_pbx_cache

    reset_dect_cache()
    reset_pbx_cache()
    dect = get_dect()
    ext = make_ext(event, "4242", owner=user, etype=ExtensionType.DECT, dect_encryption=True)
    dev = Device.objects.create(event=event, owner=user, type="dect", ipei="0123456789012")
    DeviceBinding.objects.create(extension=ext, device=dev)
    provision_dect_for_extension(ext)
    dev.refresh_from_db()
    assert dect.subs[dev.omm_ppn]["encryption"] is True
    ext.dect_encryption = False
    ext.save()
    provision_dect_for_extension(ext)  # update path
    assert dect.subs[dev.omm_ppn]["encryption"] is False
    reset_dect_cache()


# --------------------------------------------------------------------------- portal form

def _form(ext, user, data=None, files=None):
    from apps.portal.forms import ExtensionEditForm

    base = {"display_name": "Alice", "description": "", "location_hint": "", "in_phonebook": "on",
            "ring_strategy": "parallel", "ring_timeout": 30, "forward_unconditional": "", "forward_busy": "",
            "forward_noanswer": "", "allow_callback": "on", "language": "", "call_waiting": "on",
            "display_mode": "name", "forward_mode": "off", "forward_target": "", "forward_delay": 10}
    base.update(data or {})
    return ExtensionEditForm(base, files or {}, instance=Extension.objects.get(pk=ext.pk), user=user)


def test_edit_form_rejects_bad_tone_and_accepts_good_one(event, user):
    ext = make_ext(event, "4242", owner=user)
    f = _form(ext, user, files={"ringback_tone": upload("ring.mp3", make_wav())})
    assert not f.is_valid() and "ringback_tone" in f.errors
    f = _form(ext, user, files={"ringback_tone": upload("ring.wav", make_wav())})
    assert f.is_valid(), f.errors


def test_edit_form_forward_target_choices_and_typed_number(event, user, other_user, orga):
    ext = make_ext(event, "4242", owner=user)
    mine = make_ext(event, "4300", owner=user)
    theirs = make_ext(event, "4301", owner=other_user)
    f = _form(ext, user)
    assert set(f.fields["forward_target"].queryset) == {mine}
    assert set(_form(ext, orga).fields["forward_target"].queryset) == {mine, theirs}
    # mode without target
    f = _form(ext, user, {"forward_mode": "always"})
    assert not f.is_valid() and "forward_target" in f.errors
    # typed number resolves to the FK even if not in the user's own list
    f = _form(ext, user, {"forward_mode": "delayed", "forward_target_number": "4301", "forward_delay": 5})
    assert f.is_valid(), f.errors
    assert f.cleaned_data["forward_target"] == theirs and f.cleaned_data["forward_delay"] == 5
    f = _form(ext, user, {"forward_mode": "always", "forward_target_number": "4242"})
    assert not f.is_valid() and "itself" in str(f.errors)
    f = _form(ext, user, {"forward_mode": "always", "forward_target_number": "4999"})
    assert not f.is_valid() and "forward_target_number" in f.errors


def test_edit_view_saves_toggles_forwarding_and_tone(client, event, user):
    from django.urls import reverse

    ext = make_ext(event, "4242", owner=user)
    target = make_ext(event, "4300", owner=user)
    client.force_login(user)
    url = reverse("portal:extension_edit", args=[event.slug, ext.pk])
    with mock.patch.object(audio.shutil, "which", return_value=None):
        r = client.post(url, {
            "display_name": "Alice", "in_phonebook": "on", "ring_strategy": "parallel", "ring_timeout": 30,
            "allow_callback": "on", "language": "de", "display_mode": "number", "forward_mode": "delayed",
            "forward_target": str(target.pk), "forward_delay": 15, "ringback_tone": upload("ring.wav", make_wav()),
        })
    assert r.status_code == 302, r.content
    ext.refresh_from_db()
    assert ext.language == "de" and ext.display_mode == "number" and ext.call_waiting is False
    assert ext.forward_mode == "delayed" and ext.forward_target == target and ext.forward_delay == 15
    assert ext.ringback_tone_status == Extension.RingbackStatus.READY
    detail = client.get(reverse("portal:extension_detail", args=[event.slug, ext.pk]))
    assert detail.status_code == 200 and b"<audio" in detail.content and b"4300" in detail.content
    # the target's page lists who forwards to it
    detail = client.get(reverse("portal:extension_detail", args=[event.slug, target.pk]))
    assert b"Forwarded from" in detail.content
    # removing the tone via the clear checkbox
    r = client.post(url, {"display_name": "Alice", "ring_strategy": "parallel", "ring_timeout": 30,
                          "forward_mode": "off", "ringback_tone-clear": "on"})
    assert r.status_code == 302
    ext.refresh_from_db()
    assert ext.ringback_tone_status == Extension.RingbackStatus.NONE and not ext.ringback_tone
