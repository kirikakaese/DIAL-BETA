"""Phone-side call forwarding via feature codes (*21/*22/*23 + number, *20 = off)."""
import pytest
from rest_framework.test import APIClient

from apps.core.models import AuditLog
from apps.extensions import feature_codes, services
from apps.extensions.models import Extension

pytestmark = pytest.mark.django_db

SECRET = "hook-secret-123"
HDR = {"HTTP_X_DIAL_PBX_SECRET": SECRET}
URL = "/api/v1/pbx/hooks/feature-code/"


@pytest.fixture(autouse=True)
def _secret(settings):
    settings.DIAL_PBX_HOOK_SECRET = SECRET


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def alice(event, user, member):
    return services.register(event, user, "4242", "dect")


@pytest.fixture
def bob(event, other_user):
    from apps.events.models import EventMembership

    EventMembership.objects.create(event=event, user=other_user, role="user")
    return services.register(event, other_user, "4300", "sip")


def post(api, caller, code, target=""):
    r = api.post(URL, {"event": "demo", "caller": caller, "code": code, "target": target}, **HDR)
    assert r.status_code == 200
    return r.json()["handled"]


def test_set_always_via_hook_is_audited_and_provisioned(api, alice, bob):
    assert post(api, "4242", "*21", "4300") is True
    alice.refresh_from_db()
    assert alice.forward_mode == "always" and alice.forward_target == bob
    entry = AuditLog.objects.filter(action="update", target_id=str(alice.pk)).latest("created_at")
    assert entry.actor == alice.owner
    assert entry.changes["forward_mode"] == ["off", "always"] and entry.changes["forward_target"] == [None, "4300"]


def test_busy_and_noanswer_modes(api, alice, bob):
    assert post(api, "4242", "*22", "4300") is True
    alice.refresh_from_db()
    assert alice.forward_mode == "busy" and alice.forward_target == bob
    assert post(api, "4242", "*23", "4300") is True
    alice.refresh_from_db()
    assert alice.forward_mode == "noanswer"


def test_clear_switches_everything_off(api, alice, bob):
    services.set_forwarding(alice, alice.owner, mode="always", target=bob)
    assert post(api, "4242", "*20") is True
    alice.refresh_from_db()
    assert alice.forward_mode == "off" and alice.forward_target is None
    # clearing when nothing is set is still "handled" (idempotent)
    assert post(api, "4242", "*20") is True


def test_invalid_target_and_missing_target(api, alice):
    assert post(api, "4242", "*21", "9876") is False  # not a live extension
    assert post(api, "4242", "*21", "") is False  # set code needs a number
    alice.refresh_from_db()
    assert alice.forward_mode == "off"


def test_unknown_caller_and_self_forward(api, alice, bob):
    assert post(api, "1111", "*21", "4300") is False
    assert post(api, "4242", "*21", "4242") is False  # self
    alice.refresh_from_db()
    assert alice.forward_mode == "off"


def test_loop_rejected(api, alice, bob):
    services.set_forwarding(bob, bob.owner, mode="always", target=alice)
    assert post(api, "4242", "*21", "4300") is False
    alice.refresh_from_db()
    assert alice.forward_mode == "off"


def test_disabled_code_and_other_modules_still_handle(api, alice, bob, monkeypatch):
    plan = services.get_plan(alice.event)
    plan.forward_busy_code = ""
    plan.save()
    assert post(api, "4242", "*22", "4300") is False
    # *66 is still routed to the callback app
    seen = []
    monkeypatch.setattr("apps.callback.services.handle_feature_code",
                        lambda ev, caller, code, target: seen.append(code) or True)
    assert post(api, "4242", "*66", "4300") is True
    assert seen == ["*66"]
    assert feature_codes.handle_feature_code(alice.event, "4242", "*66", "4300") is False


def test_code_modes_and_direct_call(event, alice, bob):
    plan = services.get_plan(event)
    assert feature_codes.code_modes(plan) == {"*21": "always", "*22": "busy", "*23": "noanswer", "*20": "off"}
    assert feature_codes.handle_feature_code(event, "4242", "*21", "4300") is True
    assert Extension.objects.get(pk=alice.pk).forward_target == bob
    assert feature_codes.handle_feature_code(event, "4242", "", "4300") is False
    assert feature_codes.handle_feature_code(event, "4242", "*99", "4300") is False
