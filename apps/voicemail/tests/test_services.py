"""Voicemail services: mailbox provisioning, message intake, MWI, e-mail, read/delete."""
import pytest
from django.core import mail

from apps.devices.models import Device, DeviceBinding
from apps.pbx.models import VoicemailUser
from apps.voicemail import services
from apps.voicemail.models import Mailbox, Message, MessageDelivery
from apps.voicemail.services import VoicemailError

pytestmark = pytest.mark.django_db


def test_ensure_mailbox_writes_realtime_row(event, ext_alice):
    mb = services.ensure_mailbox(ext_alice)
    assert mb.event == event and mb.pin.isdigit() and len(mb.pin) == 4 and mb.enabled
    assert services.ensure_mailbox(ext_alice) == mb
    row = VoicemailUser.objects.get(context="pet-demo", mailbox="4242")
    assert row.password == mb.pin and row.fullname == "alice" and row.attach == "no" and row.maxmsg == 50
    ext_alice.refresh_from_db()
    assert ext_alice.config["voicemail_pin"] == mb.pin


def test_update_mailbox_and_pin_validation(event, ext_alice, user):
    mb = services.ensure_mailbox(ext_alice)
    services.update_mailbox(mb, actor=user, pin="123456", email_delivery=True, email="vm@example.org", max_messages=10)
    row = VoicemailUser.objects.get(context="pet-demo", mailbox="4242")
    assert row.password == "123456" and row.email == "vm@example.org" and row.attach == "yes" and row.maxmsg == 10
    with pytest.raises(VoicemailError):
        services.update_mailbox(mb, pin="12")
    with pytest.raises(VoicemailError):
        services.update_mailbox(mb, pin="abcd")
    services.update_mailbox(mb, enabled=False)
    assert not VoicemailUser.objects.filter(context="pet-demo", mailbox="4242").exists()
    services.update_mailbox(mb, enabled=True)
    assert VoicemailUser.objects.filter(context="pet-demo", mailbox="4242").exists()


def test_store_message_copies_audio_and_sets_mwi(event, ext_alice, ext_bob, wav_file, pbx):
    msg = services.store_message(event, "4242", "4300", str(wav_file), 7)
    assert msg is not None and msg.mailbox.extension == ext_alice
    assert msg.caller_number == "4300" and msg.caller_name == "Bob Builder" and msg.duration_seconds == 7
    assert msg.has_audio and msg.audio.name.startswith("voicemail/demo/4242/") and "msg0000" in msg.audio.name
    with msg.audio.open("rb") as fh:
        assert fh.read(4) == b"RIFF"
    assert msg.content_type == "audio/wav"
    assert pbx.mwi[-1] == ("4242", 1, 0)
    assert services.unread_count(msg.mailbox) == 1 and services.unread_count(ext_alice) == 1
    # a repeated notify for the same spool file is not stored twice
    assert services.store_message(event, "4242", "4300", str(wav_file), 7) == msg
    assert Message.objects.count() == 1
    # mailbox@context notation from Asterisk is accepted
    msg2 = services.store_message(event, "4242@pet-demo", "", "", 3)
    assert msg2.mailbox == msg.mailbox and msg2.caller_number == ""
    assert pbx.mwi[-1] == ("4242", 2, 0)


def test_store_message_without_file_logs_warning(event, ext_alice, pbx, caplog):
    msg = services.store_message(event, "4242", "4300", "/nonexistent/spool/msg0001.wav", 5)
    assert msg is not None and not msg.has_audio and msg.asterisk_msg_id == "/nonexistent/spool/msg0001.wav"
    assert any("not found" in r.message for r in caplog.records)
    assert pbx.mwi[-1] == ("4242", 1, 0)


def test_store_message_unknown_mailbox_or_flag_off(event, ext_alice, settings):
    assert services.store_message(event, "1111", "4300", "", 1) is None
    settings.PET_FEATURES = dict(settings.PET_FEATURES, voicemail=False)
    assert services.store_message(event, "4242", "4300", "", 1) is None


def test_dect_mwi(event, ext_alice, pbx, dect):
    dev = Device.objects.create(event=event, type="dect", ipei="0000000000001", omm_ppn="17")
    DeviceBinding.objects.create(extension=ext_alice, device=dev)
    msg = services.store_message(event, "4242", "4300", "", 2)
    assert dect.mwi["17"] == 1
    services.mark_read(msg)
    assert dect.mwi["17"] == 0 and pbx.mwi[-1] == ("4242", 0, 1)


def test_email_delivery(event, ext_alice, wav_file, user):
    mb = services.ensure_mailbox(ext_alice)
    services.update_mailbox(mb, email_delivery=True)
    msg = services.store_message(event, "4242", "4300", str(wav_file), 4)
    assert len(mail.outbox) == 1
    m = mail.outbox[0]
    assert m.to == ["alice@example.org"] and "4242" in m.subject and "4300" in m.subject
    assert len(m.attachments) == 1 and "msg0000" in m.attachments[0][0] and m.attachments[0][2] == "audio/wav"
    assert MessageDelivery.objects.get(message=msg).ok
    # override address
    services.update_mailbox(mb, email="other@example.org")
    services.store_message(event, "4242", "4300", "", 4)
    assert mail.outbox[-1].to == ["other@example.org"] and mail.outbox[-1].attachments == []


def test_mark_read_delete_and_limit(event, ext_alice, pbx, wav_file):
    mb = services.ensure_mailbox(ext_alice)
    services.update_mailbox(mb, max_messages=2)
    m1 = services.store_message(event, "4242", "4300", str(wav_file), 1)
    services.mark_read(m1)
    m1.refresh_from_db()
    assert m1.is_read and pbx.mwi[-1] == ("4242", 0, 1)
    services.mark_read(m1, False)
    assert pbx.mwi[-1] == ("4242", 1, 0)
    services.mark_read(m1)
    services.store_message(event, "4242", "4300", "", 1)
    services.store_message(event, "4242", "4300", "", 1)  # exceeds max -> oldest read one dropped
    assert not Message.objects.filter(pk=m1.pk).exists() and mb.messages.count() == 2
    m = mb.messages.first()
    services.delete_message(m)
    assert mb.messages.count() == 1 and pbx.mwi[-1] == ("4242", 1, 0)


def test_mailboxes_for_and_summary(event, user, other_user, ext_alice, ext_bob, orga):
    boxes = services.mailboxes_for(user, event)
    assert [b.number for b in boxes] == ["4242"]
    services.store_message(event, "4242", "4300", "", 1)
    services.store_message(event, "4300", "4242", "", 1)
    s = services.event_summary(event)
    assert s["mailboxes"] == 2 and s["messages"] == 2 and s["unread"] == 2
    assert services.can_access(user, boxes[0]) and services.can_access(orga, boxes[0])
    assert not services.can_access(other_user, boxes[0])


def test_hook_dispatch_end_to_end(client, event, ext_alice, wav_file, pbx):
    from apps.pbx.api import hook_secret

    r = client.post("/api/v1/pbx/hooks/voicemail/", {"event": "demo", "mailbox": "4242", "caller": "4300",
                                                     "file_path": str(wav_file), "duration": "7.4"},
                    HTTP_X_PET_PBX_SECRET=hook_secret())
    assert r.status_code == 200 and r.json()["handled"] is True
    assert Message.objects.get().duration_seconds == 7 and pbx.mwi[-1] == ("4242", 1, 0)
    assert Mailbox.objects.filter(extension=ext_alice).exists()
