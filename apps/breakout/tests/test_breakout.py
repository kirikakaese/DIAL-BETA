import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.breakout import services
from apps.breakout.models import BreakoutPermission, BreakoutUsage, CallerIdMapping, OutboundRule, Trunk
from apps.events.models import EventMembership
from apps.extensions.models import Extension
from apps.extensions.services import register

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


@pytest.fixture
def bo_event(event):
    event.allow_breakout = True
    event.save()
    return event


@pytest.fixture
def trunk(bo_event):
    return Trunk.objects.create(event=bo_event, name="sipgate", sip_host="sipgate.example", outbound_prefix="0",
                                caller_id_default="+4930123456", auth_user="u", auth_password="p")


@pytest.fixture
def ext(bo_event, user):
    return register(bo_event, user, "4242", "dect")


@pytest.fixture
def allowed(bo_event, ext):
    return BreakoutPermission.objects.create(event=bo_event, extension=ext, daily_minutes_limit=10)


def test_authorize_happy_path(bo_event, trunk, ext, allowed):
    ok, reason = services.authorize(bo_event, ext, "0030123456")
    assert ok and reason == "ok"
    assert services.dial_string(trunk, "0030123456") == f"PJSIP/030123456@trunk-{trunk.pk}"
    assert services.caller_id_for(ext, trunk) == "+4930123456"
    CallerIdMapping.objects.create(trunk=trunk, extension=ext, caller_id="+4930999")
    assert services.caller_id_for(ext, trunk) == "+4930999"


def test_authorize_denials(bo_event, trunk, ext, allowed, settings):
    assert services.authorize(bo_event, ext, "5555") == (False, "no trunk for this prefix")
    assert services.authorize(bo_event, ext, "00900123")[1].startswith("destination blocked")
    assert services.authorize(bo_event, ext, "000441234")[1].startswith("destination blocked")  # "00" international
    settings.DIAL_BREAKOUT_BLOCKLIST = []
    assert services.authorize(bo_event, ext, "000441234")[0] is True
    trunk.enabled = False
    trunk.save()
    assert services.authorize(bo_event, ext, "0030123")[0] is False


def test_flag_and_event_switch(bo_event, trunk, ext, allowed, settings):
    bo_event.allow_breakout = False
    bo_event.save()
    assert services.authorize(bo_event, ext, "0030123") == (False, "breakout not allowed for this event")
    bo_event.allow_breakout = True
    bo_event.save()
    settings.DIAL_FEATURES = {**settings.DIAL_FEATURES, "breakout": False}
    assert services.authorize(bo_event, ext, "0030123") == (False, "breakout disabled")


def test_permissions_extension_and_group(bo_event, trunk, ext, user, angels):
    assert services.authorize(bo_event, ext, "0030123") == (False, "extension not permitted for breakout")
    m = EventMembership.objects.create(event=bo_event, user=user, role="user")
    m.groups.add(angels)
    BreakoutPermission.objects.create(event=bo_event, user_group=angels, daily_minutes_limit=0)
    assert services.authorize(bo_event, ext, "0030123")[0] is True
    BreakoutPermission.objects.create(event=bo_event, extension=ext, allowed=False)
    assert services.authorize(bo_event, ext, "0030123")[0] is False  # extension-specific deny wins


def test_rules(bo_event, trunk, ext, allowed):
    OutboundRule.objects.create(trunk=trunk, name="national", pattern=r"0[1-9]\d+", allow=True, priority=50)
    OutboundRule.objects.create(trunk=trunk, name="no mobile", pattern=r"01[5-7]\d+", allow=False, priority=10)
    assert services.authorize(bo_event, ext, "0030123456")[0] is True
    assert services.authorize(bo_event, ext, "00151234567") == (False, "denied by rule no mobile")
    assert services.authorize(bo_event, ext, "0112") == (False, "no allow rule matches")


def test_quota_and_usage(bo_event, trunk, ext, allowed):
    services.record_usage(bo_event, ext, 61)
    services.record_usage(bo_event, ext, 8 * 60)
    row = BreakoutUsage.objects.get(extension=ext)
    assert row.calls == 2 and row.minutes == 10
    assert services.authorize(bo_event, ext, "0030123") == (False, "daily minute quota exhausted")


def test_rate_limit(bo_event, trunk, ext, allowed, settings):
    settings.DIAL_BREAKOUT_RATE_LIMIT = 2
    assert services.authorize(bo_event, ext, "0030123")[0]
    assert services.authorize(bo_event, ext, "0030123")[0]
    assert services.authorize(bo_event, ext, "0030123") == (False, "rate limit: too many breakout calls")


def test_pjsip_config(trunk):
    conf = services.pjsip_trunk_config(trunk)
    assert f"[trunk-{trunk.pk}]" in conf and "type=registration" in conf and "username=u" in conf
    assert "callerid=\"DIAL\" <+4930123456>" in conf


def test_pbx_route_breakout_extension(client, bo_event, trunk, orga, settings):
    bo = Extension.objects.create(event=bo_event, owner=orga, number="0800", type="breakout", state="active",
                                  config={"trunk": trunk.endpoint_name})
    BreakoutPermission.objects.create(event=bo_event, extension=bo, daily_minutes_limit=0)
    settings.DIAL_PBX_HOOK_SECRET = "s3cret"
    d = client.get("/api/v1/pbx/route/?event=demo&number=0800&destination=0030123",
                   HTTP_X_DIAL_PBX_SECRET="s3cret").json()
    assert d["breakout"]["allowed"] is True and d["targets"] == [f"PJSIP/0030123@trunk-{trunk.pk}"]


def test_views(client, bo_event, user, orga, member, trunk, ext):
    client.force_login(user)
    assert client.get(reverse("breakout:index", args=[bo_event.slug])).status_code == 403
    client.force_login(orga)
    r = client.get(reverse("breakout:index", args=[bo_event.slug]))
    assert r.status_code == 200 and "sipgate" in r.content.decode()
    r = client.post(reverse("breakout:rule_new", args=[bo_event.slug]),
                    {"trunk": trunk.pk, "name": "all", "pattern": r"\d+", "allow": "on", "per_call_max_minutes": 0,
                     "priority": 100})
    assert r.status_code == 302 and OutboundRule.objects.count() == 1
    r = client.post(reverse("breakout:permission_new", args=[bo_event.slug]),
                    {"extension": str(ext.pk), "allowed": "on", "daily_minutes_limit": 30})
    assert r.status_code == 302 and BreakoutPermission.objects.filter(extension=ext).exists()
    r = client.post(reverse("breakout:trunk_new", args=[bo_event.slug]),
                    {"name": "t2", "sip_host": "h", "port": 5060, "transport": "udp", "outbound_prefix": "00",
                     "enabled": "on"})
    assert r.status_code == 302 and Trunk.objects.count() == 2


def test_api(client, bo_event, user, orga, member, trunk, ext):
    client.force_login(user)
    assert client.get("/api/v1/breakout/trunks/?event=demo").status_code == 403
    client.force_login(orga)
    r = client.get("/api/v1/breakout/trunks/?event=demo").json()
    rows = r["results"] if isinstance(r, dict) else r
    assert len(rows) == 1 and "auth_password" not in rows[0]
    r = client.post("/api/v1/breakout/rules/", {"trunk": trunk.pk, "name": "x", "pattern": r"\d+", "allow": True},
                    content_type="application/json")
    assert r.status_code == 201
    r = client.post("/api/v1/breakout/permissions/", {"event": "demo", "extension": str(ext.pk), "allowed": True},
                    content_type="application/json")
    assert r.status_code == 201
    services.record_usage(bo_event, ext, 120)
    r = client.get("/api/v1/breakout/usage/?event=demo").json()
    assert r[0]["extension"] == "4242" and r[0]["minutes"] == 2
