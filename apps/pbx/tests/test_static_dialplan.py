"""Cross-checks between the Python dialplan generator and the static ``extensions.conf``.

The realtime rows written by :mod:`apps.pbx.dialplan` jump into static contexts that live in
``deploy/asterisk/conf/extensions.conf``. These tests make sure both sides agree on context
names, service extens and the originate targets used by :meth:`AsteriskPBX.originate`.
"""
import re
from pathlib import Path

import pytest

from apps.extensions.models import ExtensionType
from apps.extensions.services import get_plan
from apps.pbx import dialplan as dp

from .conftest import make_extension

ROOT = Path(__file__).resolve().parents[3]
CONF = ROOT / "deploy" / "asterisk" / "conf" / "extensions.conf"

# every static context the generator / API layer / entrypoint refers to
REQUIRED_CONTEXTS = {
    "dial-internal", "dial-services", "dial-group", "dial-ivr", "dial-app", "dial-route", "dial-feature",
    "dial-hangup", "dial-emergency", "dial-hook", "dial-lookup", "dial-dial", "dial-after-dial",
}
# ``DIAL_SERVICE`` values other apps pass to ``originate()`` (+ the default ``announce``)
ORIGINATE_TARGETS = {"announce", "callback", "ringback", "wakeup-call"}


@pytest.fixture(scope="module")
def conf_text():
    assert CONF.exists(), f"missing {CONF}"
    return CONF.read_text()


def contexts(text) -> set[str]:
    return set(re.findall(r"^\[([a-z0-9_-]+)\]", text, flags=re.M))


def extens_in(text, context) -> set[str]:
    m = re.search(rf"^\[{re.escape(context)}\]\n(.*?)(?=^\[|\Z)", text, flags=re.M | re.S)
    assert m, f"context {context} not found"
    return set(re.findall(r"^exten => ([^,]+),", m.group(1), flags=re.M))


def test_static_contexts_exist(conf_text):
    missing = REQUIRED_CONTEXTS - contexts(conf_text)
    assert not missing, f"extensions.conf lacks contexts {sorted(missing)}"


def test_service_numbers_map_to_static_extens(event, conf_text):
    services = extens_in(conf_text, dp.SERVICES_CONTEXT)
    for _field, svc in dp.SERVICE_NUMBERS:
        assert svc in services, f"[dial-services] has no exten {svc}"
    for target in ORIGINATE_TARGETS:
        assert target in services, f"[dial-services] has no originate target {target}"
    # what rows_for_plan actually emits for the demo plan
    for row in dp.rows_for_plan(event, get_plan(event)):
        if row.app == "Goto" and row.appdata.startswith(f"{dp.SERVICES_CONTEXT},"):
            assert row.appdata.split(",")[1] in services


def test_gosub_targets_exist(event, user, conf_text):
    ctxs = contexts(conf_text)
    exts = [
        make_extension(event, "4400", etype=ExtensionType.GROUP, owner=user),
        make_extension(event, "4700", etype=ExtensionType.APP, owner=user, config={"app": "echo"}),
        make_extension(event, "4800", etype=ExtensionType.IVR, owner=user),
        make_extension(event, "4801", etype=ExtensionType.ANNOUNCEMENT, owner=user),
        make_extension(event, "4900", etype=ExtensionType.FEDERATION, owner=user),
        make_extension(event, "4901", etype=ExtensionType.BREAKOUT, owner=user),
    ]
    for ext in exts:
        for row in dp.rows_for_extension(ext):
            if row.app == "Gosub":
                ctx = row.appdata.split(",")[0]
                assert ctx in ctxs, f"{ext.type}: Gosub into unknown context {ctx}"
                assert "s" in extens_in(conf_text, ctx)
    for row in dp.rows_for_plan(event, get_plan(event)):
        if row.app == "Goto":
            ctx = row.appdata.split(",")[0]
            assert ctx in ctxs, f"plan row Goto into unknown context {ctx}"


def test_hangup_handler_and_hook_urls(conf_text):
    ctx, exten, prio = dp.HANGUP_HANDLER.split(",")
    assert exten in extens_in(conf_text, ctx) and prio == "1"
    # the static dialplan must call exactly the hook kinds / route URL DIAL exposes
    for kind in ("feature-code", "extension-idle", "cdr", "site-survey", "dect-claim"):
        assert f"/api/v1/pbx/hooks/{kind}/" in conf_text or f"(dial-hook,s,1({kind}," in conf_text
    assert "/api/v1/pbx/route/?event=${DIAL_EVENT}&number=" in conf_text
    assert "X-DIAL-PBX-Secret" in conf_text
    # the app_voicemail notify script posts to the voicemail hook
    vm = (ROOT / "deploy" / "asterisk" / "scripts" / "vm_notify.sh").read_text()
    assert "/api/v1/pbx/hooks/voicemail/" in vm and "X-DIAL-PBX-Secret" in vm


def test_confbridge_profiles_exist():
    text = (ROOT / "deploy" / "asterisk" / "conf" / "confbridge.conf").read_text()
    assert "[dial_bridge]" in text and "[dial_user]" in text


def test_realtime_tables_match_models():
    """extconfig.conf / sorcery.conf / cdr_adaptive_odbc.conf must use the Django db_table names."""
    from apps.pbx import models as m

    extconfig = (ROOT / "deploy" / "asterisk" / "conf" / "extconfig.conf").read_text()
    sorcery = (ROOT / "deploy" / "asterisk" / "conf" / "sorcery.conf").read_text()
    cdr = (ROOT / "deploy" / "asterisk" / "conf" / "cdr_adaptive_odbc.conf").read_text()
    for model in (m.PsEndpoint, m.PsAuth, m.PsAor, m.PsContact, m.PsEndpointIdIp, m.DialplanEntry):
        table = model._meta.db_table
        assert re.search(rf"^{table} => odbc,asterisk,{table}$", extconfig, flags=re.M), table
    assert re.search(rf"^voicemail => odbc,asterisk,{m.VoicemailUser._meta.db_table}$", extconfig, flags=re.M)
    for obj, model in (("endpoint", m.PsEndpoint), ("auth", m.PsAuth), ("aor", m.PsAor), ("contact", m.PsContact)):
        assert f"{obj}=realtime,{model._meta.db_table}" in sorcery
    assert re.search(rf"^table={m.Cdr._meta.db_table}$", cdr, flags=re.M)
