"""Dialplan/endpoint output for the per-extension features: forwarding modes, ringback tone, language,
caller-ID display mode and call waiting."""
import pytest
from django.core.files.base import ContentFile

from apps.extensions.models import Extension
from apps.pbx import dialplan as dp
from apps.pbx.models import DialplanEntry, PsEndpoint

from .conftest import bind, make_device, make_extension

pytestmark = pytest.mark.django_db


def rows(ctx, exten):
    return list(DialplanEntry.objects.filter(context=ctx, exten=exten).order_by("priority"))


def apps_of(entries):
    return [(r.app, r.appdata) for r in entries]


def find(entries, app):
    return [r for r in entries if r.app == app]


def _row_at(entries, prio):
    return next(r for r in entries if r.priority == prio)


@pytest.fixture
def target(event, user):
    return make_extension(event, "4300", owner=user, display_name="Target")


@pytest.fixture
def ext(event, user):
    e = make_extension(event, "4242", owner=user, display_name="Alice")
    bind(e, make_device(event, "demo-aaaa"))
    return e


# --------------------------------------------------------------------------- forwarding

def test_forward_always_skips_dial(pbx, ext, target):
    ext.forward_mode = Extension.ForwardMode.ALWAYS
    ext.forward_target = target
    ext.save()
    pbx.sync_extension(ext)
    r = rows("dial-demo", "4242")
    assert not find(r, "Dial")
    assert find(r, "Goto")[0].appdata == "dial-demo,4300,1"


def test_forward_delayed_rings_then_forwards(pbx, ext, target):
    ext.forward_mode = Extension.ForwardMode.DELAYED
    ext.forward_delay = 12
    ext.forward_target = target
    ext.save()
    pbx.sync_extension(ext)
    r = rows("dial-demo", "4242")
    conf = [x.as_conf_line() for x in r]
    assert conf == [
        "exten => 4242,1,Set(__DIAL_EVENT=demo)",
        "exten => 4242,2,Set(DIAL_EXTEN=4242)",
        "exten => 4242,3,Set(DIAL_ALLOW_CB=1)",
        "exten => 4242,4,Set(DIAL_PRIORITY=0)",
        "exten => 4242,5,Set(CHANNEL(hangup_handler_push)=dial-hangup,s,1)",
        "exten => 4242,6,Set(CHANNEL(language)=en)",
        "exten => 4242,7,Dial(PJSIP/demo-aaaa,12,tT)",
        'exten => 4242,8,GotoIf($["${DIALSTATUS}" = "BUSY"]?11)',
        "exten => 4242,9,Goto(dial-demo,4300,1)",
        "exten => 4242,10,Hangup()",
        "exten => 4242,11,Goto(dial-demo,4300,1)",
        "exten => 4242,12,Hangup()",
    ]
    assert not find(r, "VoiceMail")


def test_forward_busy_only_in_busy_branch(pbx, ext, target):
    ext.forward_mode = Extension.ForwardMode.BUSY
    ext.forward_target = target
    ext.save()
    pbx.sync_extension(ext)
    r = rows("dial-demo", "4242")
    assert find(r, "Dial")[0].appdata == "PJSIP/demo-aaaa,30,tT"
    busy_prio = int(find(r, "GotoIf")[0].appdata.rsplit("?", 1)[1])
    assert _row_at(r, busy_prio).app == "Goto" and _row_at(r, busy_prio).appdata == "dial-demo,4300,1"
    assert [v.appdata for v in find(r, "VoiceMail")] == ["4242@dial-demo,u"]


def test_forward_noanswer_only_in_noanswer_branch(pbx, ext, target):
    ext.forward_mode = Extension.ForwardMode.NOANSWER
    ext.forward_target = target
    ext.save()
    pbx.sync_extension(ext)
    r = rows("dial-demo", "4242")
    dial_prio = find(r, "Dial")[0].priority
    assert _row_at(r, dial_prio + 2).appdata == "dial-demo,4300,1"  # right after the GotoIf
    assert [v.appdata for v in find(r, "VoiceMail")] == ["4242@dial-demo,b"]


def test_fk_forwarding_takes_precedence_over_legacy_fields(pbx, ext, target):
    ext.forward_unconditional = "4999"
    ext.forward_busy = "4998"
    ext.forward_mode = Extension.ForwardMode.BUSY
    ext.forward_target = target
    ext.save()
    pbx.sync_extension(ext)
    r = rows("dial-demo", "4242")
    assert find(r, "Dial")  # legacy unconditional ignored while a FK mode is set
    assert {g.appdata for g in find(r, "Goto")} == {"dial-demo,4300,1"}


def test_legacy_fields_still_apply_when_mode_off(pbx, ext):
    ext.forward_noanswer = "4998"
    ext.save()
    pbx.sync_extension(ext)
    assert find(rows("dial-demo", "4242"), "Goto")[0].appdata == "dial-demo,4998,1"


def test_forward_mode_without_target_is_ignored(pbx, ext):
    ext.forward_mode = Extension.ForwardMode.ALWAYS
    ext.save()
    pbx.sync_extension(ext)
    r = rows("dial-demo", "4242")
    assert find(r, "Dial") and not find(r, "Goto")


# --------------------------------------------------------------------------- ringback / language

def _ready_tone(ext):
    ext.ringback_tone.save("ring.wav", ContentFile(b"RIFF....WAVEfmt "), save=False)
    ext.ringback_tone_processed.save("tone.wav", ContentFile(b"RIFF....WAVEfmt "), save=False)
    ext.ringback_tone_status = Extension.RingbackStatus.READY
    ext.save()


def test_ready_ringback_tone_adds_moh_dial_option(pbx, ext):
    _ready_tone(ext)
    pbx.sync_extension(ext)
    assert find(rows("dial-demo", "4242"), "Dial")[0].appdata == "PJSIP/demo-aaaa,30,tTm(dial-demo-4242)"


def test_pending_or_failed_tone_does_not_change_dial(pbx, ext):
    _ready_tone(ext)
    ext.ringback_tone_status = Extension.RingbackStatus.FAILED
    ext.save()
    pbx.sync_extension(ext)
    assert find(rows("dial-demo", "4242"), "Dial")[0].appdata == "PJSIP/demo-aaaa,30,tT"


def test_render_musiconhold_and_dialplan_hint(pbx, event, ext):
    assert pbx.render_musiconhold(event).count("[dial-demo-") == 0
    _ready_tone(ext)
    moh = pbx.render_musiconhold(event)
    assert "[dial-demo-4242]\nmode=files\ndirectory=" in moh
    directory = next(line for line in moh.splitlines() if line.startswith("directory=")).split("=", 1)[1]
    assert directory.endswith(f"ringback/processed/{ext.pk}")
    text = pbx.render_dialplan(event)
    assert "tTm(dial-demo-4242)" in text and "dial-moh.conf" in text


def test_language_preamble(pbx, ext):
    pbx.sync_extension(ext)
    assert ("Set", "CHANNEL(language)=en") in apps_of(rows("dial-demo", "4242"))  # event default
    ext.language = "de"
    ext.save()
    pbx.sync_extension(ext)
    assert ("Set", "CHANNEL(language)=de") in apps_of(rows("dial-demo", "4242"))
    assert dp.announcement_language(ext) == "de"


# --------------------------------------------------------------------------- endpoint toggles

@pytest.mark.parametrize("mode, expected", [
    ("number_name", '"4242 Alice" <4242>'),
    ("name", '"Alice" <4242>'),
    ("number", '"4242" <4242>'),
])
def test_callerid_per_display_mode(pbx, ext, mode, expected):
    ext.display_mode = mode
    ext.save()
    dev = ext.bindings.first().device
    pbx.sync_device(dev)
    assert PsEndpoint.objects.get(id="demo-aaaa").callerid == expected


def test_call_waiting_off_sets_busy_at_one(pbx, ext):
    dev = ext.bindings.first().device
    pbx.sync_device(dev)
    assert PsEndpoint.objects.get(id="demo-aaaa").device_state_busy_at is None
    ext.call_waiting = False
    ext.save()
    pbx.sync_device(dev)
    assert PsEndpoint.objects.get(id="demo-aaaa").device_state_busy_at == 1
