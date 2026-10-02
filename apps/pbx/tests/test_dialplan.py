"""Dialplan generation: sync_extension writes the expected realtime rows."""
import pytest

from apps.extensions.models import ExtensionType
from apps.extensions.services import get_plan
from apps.pbx import dialplan as dp
from apps.pbx.models import DialplanEntry, VoicemailUser

from .conftest import bind, make_device, make_extension

pytestmark = pytest.mark.django_db


def rows(ctx, exten):
    return list(DialplanEntry.objects.filter(context=ctx, exten=exten).order_by("priority"))


def apps_of(entries):
    return [(r.app, r.appdata) for r in entries]


def find(entries, app):
    return [r for r in entries if r.app == app]


def test_parallel_ring_rows(pbx, ext_two_devices):
    ext = ext_two_devices
    pbx.sync_extension(ext)
    r = rows("pet-demo", "4242")
    assert [x.priority for x in r] == list(range(1, len(r) + 1))
    assert apps_of(r)[:5] == [
        ("Set", "__PET_EVENT=demo"), ("Set", "PET_EXTEN=4242"), ("Set", "PET_ALLOW_CB=1"),
        ("Set", "PET_PRIORITY=0"), ("Set", "CHANNEL(hangup_handler_push)=pet-hangup,s,1"),
    ]
    dial = find(r, "Dial")
    assert len(dial) == 1
    assert dial[0].appdata == "PJSIP/demo-aaaa&PJSIP/demo-bbbb,30,tT"
    # BUSY branch jumps to a numeric priority that exists
    gotoif = find(r, "GotoIf")[0]
    target = int(gotoif.appdata.rsplit("?", 1)[1])
    assert target in {x.priority for x in r}
    # voicemail feature is on in tests -> noanswer + busy both end in VoiceMail
    vm = find(r, "VoiceMail")
    assert {v.appdata for v in vm} == {"4242@pet-demo,u", "4242@pet-demo,b"}
    assert VoicemailUser.objects.filter(context="pet-demo", mailbox="4242").exists()


def test_serial_ring_rows(pbx, ext_two_devices):
    ext = ext_two_devices
    ext.ring_strategy = "serial"
    ext.ring_timeout = 20
    ext.save()
    pbx.sync_extension(ext)
    r = rows("pet-demo", "4242")
    dials = find(r, "Dial")
    assert [d.appdata for d in dials] == ["PJSIP/demo-aaaa,20,tT", "PJSIP/demo-bbbb,20,tT"]
    # second device has a ring delay -> Wait before its Dial
    waits = find(r, "Wait")
    assert len(waits) == 1 and waits[0].appdata == "5"
    assert waits[0].priority < dials[1].priority


def test_forwarding_rows(pbx, event, user):
    ext = make_extension(event, "4300", owner=user, forward_busy="4301", forward_noanswer="4302",
                         allow_callback=False, priority=5)
    bind(ext, make_device(event, "demo-cccc"))
    pbx.sync_extension(ext)
    r = rows("pet-demo", "4300")
    assert ("Set", "PET_ALLOW_CB=0") in apps_of(r)
    assert ("Set", "PET_PRIORITY=5") in apps_of(r)
    gotos = {g.appdata for g in find(r, "Goto")}
    assert gotos == {"pet-demo,4301,1", "pet-demo,4302,1"}
    assert not find(r, "VoiceMail")


def test_forward_unconditional_skips_dial(pbx, event, user):
    ext = make_extension(event, "4310", owner=user, forward_unconditional="4242")
    bind(ext, make_device(event, "demo-dddd"))
    pbx.sync_extension(ext)
    r = rows("pet-demo", "4310")
    assert not find(r, "Dial")
    assert find(r, "Goto")[0].appdata == "pet-demo,4242,1"


def test_no_devices_falls_to_noanswer(pbx, event, user):
    ext = make_extension(event, "4320", owner=user)
    pbx.sync_extension(ext)
    r = rows("pet-demo", "4320")
    assert not find(r, "Dial")
    assert find(r, "VoiceMail")[0].appdata == "4320@pet-demo,u"


def test_group_conference_voicemail_app_rows(pbx, event, user):
    grp = make_extension(event, "4400", etype=ExtensionType.GROUP, owner=user)
    conf = make_extension(event, "4500", etype=ExtensionType.CONFERENCE, owner=user, config={"pin": "1234"})
    vm = make_extension(event, "4600", etype=ExtensionType.VOICEMAIL, owner=user)
    app = make_extension(event, "4700", etype=ExtensionType.APP, owner=user, config={"app": "time"})
    ivr = make_extension(event, "4800", etype=ExtensionType.IVR, owner=user)
    for e in (grp, conf, vm, app, ivr):
        pbx.sync_extension(e)
    assert find(rows("pet-demo", "4400"), "Gosub")[0].appdata == "pet-group,s,1(4400)"
    c = rows("pet-demo", "4500")
    assert find(c, "Authenticate")[0].appdata == "1234"
    assert find(c, "ConfBridge")[0].appdata == "pet-demo-4500,pet_bridge,pet_user"
    assert find(rows("pet-demo", "4600"), "VoiceMail")[0].appdata == "4600@pet-demo,u"
    assert find(rows("pet-demo", "4700"), "Gosub")[0].appdata == "pet-app,s,1(time)"
    assert find(rows("pet-demo", "4800"), "Gosub")[0].appdata == "pet-ivr,s,1(4800,ivr)"
    # a group has no mailbox
    assert not VoicemailUser.objects.filter(mailbox="4400").exists()


def test_conference_without_pin(pbx, event, user):
    conf = make_extension(event, "4501", etype=ExtensionType.CONFERENCE, owner=user)
    pbx.sync_extension(conf)
    assert not find(rows("pet-demo", "4501"), "Authenticate")


def test_resync_replaces_rows(pbx, ext_two_devices):
    pbx.sync_extension(ext_two_devices)
    n1 = DialplanEntry.objects.filter(context="pet-demo", exten="4242").count()
    ext_two_devices.forward_noanswer = "4243"
    ext_two_devices.save()
    pbx.sync_extension(ext_two_devices)
    r = rows("pet-demo", "4242")
    assert len(r) >= 1
    assert DialplanEntry.objects.filter(context="pet-demo", exten="4242").count() <= n1
    assert find(r, "Goto")[0].appdata == "pet-demo,4243,1"


def test_remove_extension(pbx, ext_two_devices):
    pbx.sync_extension(ext_two_devices)
    pbx.remove_extension(ext_two_devices)
    assert not DialplanEntry.objects.filter(context="pet-demo", exten="4242").exists()
    assert not VoicemailUser.objects.filter(context="pet-demo", mailbox="4242").exists()


def test_sync_event_writes_service_numbers_and_feature_codes(pbx, event, ext_two_devices):
    n = pbx.sync_event(event)
    assert n == 1
    assert find(rows("pet-demo", "9003"), "Goto")[0].appdata == "pet-services,echo,1"
    assert find(rows("pet-demo", "9000"), "Goto")[0].appdata == "pet-services,ringback-request,1"
    assert find(rows("pet-demo", "9001"), "Goto")[0].appdata == "pet-services,wakeup,1"
    assert find(rows("pet-demo", "9002"), "Goto")[0].appdata == "pet-services,survey,1"
    assert find(rows("pet-demo", "9999"), "Goto")[0].appdata == "pet-services,voicemail,1"
    # feature codes: bare and with target suffix
    assert find(rows("pet-demo", "*66"), "Gosub")[0].appdata == "pet-feature,s,1(*66,)"
    assert find(rows("pet-demo", "_*66."), "Gosub")[0].appdata == "pet-feature,s,1(*66,${EXTEN:3})"
    assert DialplanEntry.objects.filter(context="pet-demo", exten="_*72.").exists()
    # emergency numbers
    assert find(rows("pet-demo", "112"), "Goto")[0].appdata == "pet-emergency,112,1"


def test_sync_event_prunes_stale_rows(pbx, event, ext_two_devices):
    DialplanEntry.objects.create(context="pet-demo", exten="1111", priority=1, app="Hangup", appdata="")
    DialplanEntry.objects.create(context="pet-other", exten="1111", priority=1, app="Hangup", appdata="")
    pbx.sync_event(event)
    assert not DialplanEntry.objects.filter(context="pet-demo", exten="1111").exists()
    assert DialplanEntry.objects.filter(context="pet-other", exten="1111").exists()


def test_render_dialplan(pbx, event, ext_two_devices):
    text = pbx.render_dialplan(event)
    assert "[pet-demo]" in text
    assert "include => pet-internal" in text
    assert "exten => 4242,1,Set(__PET_EVENT=demo)" in text
    assert "exten => 9003,2,Goto(pet-services,echo,1)" in text
    assert "Dial(PJSIP/demo-aaaa&PJSIP/demo-bbbb,30,tT)" in text


def test_shell_context_and_plan_extens(event):
    assert dp.shell_context(event) == "[pet-demo]\ninclude => pet-internal\nswitch => Realtime/@\n"
    extens = dp.plan_extens(get_plan(event))
    assert {"9000", "9001", "9002", "9003", "9999", "*66", "_*66.", "*86", "*71", "*72", "112"} <= extens
    # forwarding feature codes (defaults *21/*22/*23 set, *20 clear) - bare and with number suffix
    assert {"*21", "_*21.", "*22", "_*22.", "*23", "_*23.", "*20", "_*20."} <= extens


def test_forward_codes_and_record_number_rows(pbx, event, ext_two_devices):
    plan = get_plan(event)
    plan.announcement_record_number = "9005"
    plan.forward_busy_code = ""  # disabled code -> no rows
    plan.save()
    pbx.sync_event(event)
    assert find(rows("pet-demo", "*21"), "Gosub")[0].appdata == "pet-feature,s,1(*21,)"
    assert find(rows("pet-demo", "_*21."), "Gosub")[0].appdata == "pet-feature,s,1(*21,${EXTEN:3})"
    assert find(rows("pet-demo", "*20"), "Gosub")[0].appdata == "pet-feature,s,1(*20,)"
    assert DialplanEntry.objects.filter(context="pet-demo", exten="_*23.").exists()
    assert not DialplanEntry.objects.filter(context="pet-demo", exten__in=["*22", "_*22."]).exists()
    # recording service number: plain + en-bloc form setting PET_RECORD_CODE
    assert find(rows("pet-demo", "9005"), "Goto")[0].appdata == "pet-services,record-announcement,1"
    suffix = rows("pet-demo", "_9005X.")
    assert ("Set", "PET_RECORD_CODE=${EXTEN:4}") in apps_of(suffix)
    assert find(suffix, "Goto")[0].appdata == "pet-services,record-announcement,1"
    extens = dp.plan_extens(plan)
    assert {"9005", "_9005X."} <= extens and "*22" not in extens


def test_clone_copies_new_plan_fields(event):
    from apps.events.models import Event

    plan = get_plan(event)
    plan.announcement_record_number = "9005"
    plan.forward_set_code = "*31"
    plan.forward_clear_code = "*30"
    plan.forward_noanswer_code = ""
    plan.save()
    new = Event.objects.create(name="Next", slug="next", start_date="2031-01-01", end_date="2031-01-02")
    cloned = plan.clone_to(new)
    assert cloned.announcement_record_number == "9005"
    assert (cloned.forward_set_code, cloned.forward_clear_code, cloned.forward_busy_code,
            cloned.forward_noanswer_code) == ("*31", "*30", "*22", "")
    assert "9005" in cloned.service_numbers() and "*31" in cloned.feature_codes()


def test_builder_unresolved_label():
    b = dp.ExtenBuilder("1")
    b.add("Goto", b.ref("nowhere"))
    with pytest.raises(ValueError):
        b.build()


# --------------------------------------------------------------------------- trunks (number blocks)

def make_trunk(event, user, number="4700", digits=2, **fields):
    return make_extension(event, number, etype=ExtensionType.TRUNK, owner=user, config={"block_digits": digits},
                          **fields)


def test_trunk_rows_base_and_pattern(pbx, event, user):
    trunk = make_trunk(event, user, ring_timeout=25)
    bind(trunk, make_device(event, "demo-pbx"))
    assert dp.trunk_endpoint(trunk) == "demo-pbx"
    assert dp.trunk_dial_string(trunk, "4711") == "PJSIP/4711@demo-pbx"
    assert dp.extens_for_extension(trunk) == {"4700", "_47XX"}
    pbx.sync_extension(trunk)
    base = rows("pet-demo", "4700")
    assert [x.priority for x in base] == list(range(1, len(base) + 1))
    assert apps_of(base)[:2] == [("Set", "__PET_EVENT=demo"), ("Set", "PET_EXTEN=4700")]
    assert ("Set", "PET_TRUNK=4700") in apps_of(base)
    assert [d.appdata for d in find(base, "Dial")] == ["PJSIP/4700@demo-pbx,25,tT"]
    pattern = rows("pet-demo", "_47XX")
    assert ("Set", "PET_EXTEN=${EXTEN}") in apps_of(pattern)
    assert ("Set", "PET_TRUNK=4700") in apps_of(pattern)
    assert [d.appdata for d in find(pattern, "Dial")] == ["PJSIP/${EXTEN}@demo-pbx,25,tT"]
    # BUSY branch jumps to the Busy() row; no voicemail for a trunk
    for r in (base, pattern):
        target = int(find(r, "GotoIf")[0].appdata.rsplit("?", 1)[1])
        assert find(r, "Busy")[0].priority == target
        assert not find(r, "VoiceMail")
    assert not VoicemailUser.objects.filter(context="pet-demo", mailbox="4700").exists()
    text = pbx.render_dialplan(event)
    assert "exten => _47XX,1,Set(__PET_EVENT=demo)" in text and "Dial(PJSIP/${EXTEN}@demo-pbx,25,tT)" in text


def test_trunk_rows_without_sip_account(pbx, event, user):
    trunk = make_trunk(event, user, "4710", 1)
    assert dp.trunk_dial_string(trunk, "4711") == ""
    pbx.sync_extension(trunk)
    r = rows("pet-demo", "_471X")
    assert not find(r, "Dial")
    assert find(r, "NoOp")[0].appdata == "PET: no SIP account bound to trunk 4710"
    assert find(r, "Playback")[0].appdata == "vm-nobodyavail"
    # a DECT handset bound by mistake is never used as trunk endpoint
    bind(trunk, make_device(event, "demo-hs", dtype="dect"))
    assert dp.trunk_endpoint(trunk) == ""


def test_remove_trunk_prunes_pattern_row(pbx, event, user):
    trunk = make_trunk(event, user)
    bind(trunk, make_device(event, "demo-pbx"))
    pbx.sync_extension(trunk)
    assert DialplanEntry.objects.filter(context="pet-demo", exten__in=["4700", "_47XX"]).count() > 2
    pbx.remove_extension(trunk)
    assert not DialplanEntry.objects.filter(context="pet-demo", exten__in=["4700", "_47XX"]).exists()


def test_sync_event_keeps_trunk_pattern_row(pbx, event, user):
    trunk = make_trunk(event, user)
    bind(trunk, make_device(event, "demo-pbx"))
    assert pbx.sync_event(event) == 1
    assert DialplanEntry.objects.filter(context="pet-demo", exten="_47XX").exists()
    # the pattern of a trunk that is no longer active is pruned
    trunk.state = "suspended"
    trunk.save()
    assert pbx.sync_event(event) == 0
    assert not DialplanEntry.objects.filter(context="pet-demo", exten__in=["4700", "_47XX"]).exists()
