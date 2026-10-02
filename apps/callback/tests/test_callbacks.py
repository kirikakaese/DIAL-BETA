"""CCBS/CCNR: feature codes, delivery on idle, cancel, expiry, retries."""
import datetime as dt

import pytest
from django.utils import timezone

from apps.callback import services, tasks
from apps.callback.models import CallbackRequest
from apps.callback.services import CallbackError
from apps.pbx.base import PBXError

pytestmark = pytest.mark.django_db


def test_request_ccbs_via_feature_code(event, ext_alice, ext_bob, pbx):
    assert services.handle_feature_code(event, "4242", "*66", "4300") is True
    req = CallbackRequest.objects.get()
    assert req.kind == "ccbs" and req.state == "pending"
    assert req.requester == ext_alice and req.target == ext_bob
    assert req.requester_number == "4242" and req.target_number == "4300"
    assert req.expires_at > timezone.now() + dt.timedelta(minutes=25)
    assert pbx.originated == []  # nothing is dialed until the target is idle


def test_ccnr_code_and_unknown_codes(event, ext_alice, ext_bob):
    assert services.handle_feature_code(event, "4242", "ccnr", "4300") is True
    assert CallbackRequest.objects.get().kind == "ccnr"
    assert services.handle_feature_code(event, "4242", "*71", "4300") is False
    assert services.handle_feature_code(event, "4242", "", "") is False
    assert services.handle_feature_code(event, "4242", "*66", "") is False  # no target


def test_duplicate_pending_is_rejected(event, ext_alice, ext_bob):
    services.request_callback(event, "4242", "4300", "ccbs")
    with pytest.raises(CallbackError):
        services.request_callback(event, "4242", "4300", "ccbs")
    assert services.handle_feature_code(event, "4242", "*66", "4300") is False
    assert CallbackRequest.objects.count() == 1


def test_validation(event, user, other_user, ext_alice, ext_bob):
    with pytest.raises(CallbackError):
        services.request_callback(event, "4242", "4242", "ccbs")
    with pytest.raises(CallbackError):
        services.request_callback(event, "4242", "9876", "ccbs")  # unknown target
    with pytest.raises(CallbackError):
        services.request_callback(event, "1111", "4300", "ccbs")  # unknown requester
    ext_bob.allow_callback = False
    ext_bob.save()
    with pytest.raises(CallbackError):
        services.request_callback(event, "4242", "4300", "ccbs")
    ext_bob.allow_callback = True
    ext_bob.save()
    # web: must own the requester extension
    with pytest.raises(CallbackError):
        services.request_callback(event, "4242", "4300", "ccbs", via="web", user=other_user)
    req = services.request_callback(event, "4242", "4300", "ccbs", via="web", user=user)
    assert req.state == "pending"


def test_orga_may_request_for_others_via_web(event, orga, ext_alice, ext_bob):
    req = services.request_callback(event, "4242", "4300", "ccbs", via="web", user=orga)
    assert req.requester_number == "4242"


def test_on_extension_idle_fires_originate(event, ext_alice, ext_bob, pbx):
    services.request_callback(event, "4242", "4300", "ccbs")
    assert services.on_extension_idle(event, "4242") == 0  # requester idle: nothing to do
    assert services.on_extension_idle(event, "4300") == 1
    o = pbx.originated[-1]
    assert o["destination"] == "4242"
    assert o["caller_id"] == "Callback 4300" and o["context"] == "pet-services"
    assert o["variables"]["PET_CALLBACK_TARGET"] == "4300"
    assert o["variables"]["PET_SERVICE"] == "callback"
    req = CallbackRequest.objects.get()
    assert o["variables"]["PET_CALLBACK_ID"] == str(req.pk)
    assert req.state == "completed" and req.attempts == 1 and req.channel_id == o["id"]
    # firing again does nothing
    assert services.on_extension_idle(event, "4300") == 0
    assert len(pbx.originated) == 1


def test_idle_skips_expired(event, ext_alice, ext_bob, pbx):
    req = services.request_callback(event, "4242", "4300", "ccbs")
    req.expires_at = timezone.now() - dt.timedelta(minutes=1)
    req.save()
    assert services.on_extension_idle(event, "4300") == 0
    req.refresh_from_db()
    assert req.state == "expired" and pbx.originated == []


def test_cancel_code(event, ext_alice, ext_bob, pbx):
    services.request_callback(event, "4242", "4300", "ccbs")
    assert services.handle_feature_code(event, "4242", "*86", "") is True
    assert CallbackRequest.objects.get().state == "cancelled"
    assert services.on_extension_idle(event, "4300") == 0
    assert pbx.originated == []


def test_expire_task(event, ext_alice, ext_bob):
    req = services.request_callback(event, "4242", "4300", "ccbs")
    fresh = services.request_callback(event, "4300", "4242", "ccbs")
    req.expires_at = timezone.now() - dt.timedelta(seconds=1)
    req.save()
    assert tasks.expire_stale_requests() == 1
    req.refresh_from_db()
    fresh.refresh_from_db()
    assert req.state == "expired" and fresh.state == "pending"


def test_pbx_error_retries_then_fails(event, ext_alice, ext_bob, pbx, monkeypatch):
    def boom(**kw):
        raise PBXError("ARI down")

    monkeypatch.setattr(pbx, "originate", boom)
    req = services.request_callback(event, "4242", "4300", "ccbs")
    now = timezone.now()
    assert services.deliver_callback(req, now=now) is False
    assert req.state == "pending" and req.attempts == 1
    assert req.next_attempt_at == now + dt.timedelta(minutes=1)
    # not yet due
    assert services.dispatch_due(now=now)["callbacks_retried"] == 0
    later = now + dt.timedelta(minutes=1)
    assert services.dispatch_due(now=later)["callbacks_retried"] == 1
    req.refresh_from_db()
    assert req.attempts == 2 and req.state == "pending"
    services.deliver_callback(req, now=later + dt.timedelta(minutes=1))
    assert req.state == "failed" and req.attempts == 3 and "ARI down" in req.note


def test_report_result_for_callback(event, ext_alice, ext_bob, pbx):
    req = services.request_callback(event, "4242", "4300", "ccbs")
    services.on_extension_idle(event, "4300")
    assert services.handle_feature_code(event, "4242", "callback-failed", str(req.pk)) is True
    req.refresh_from_db()
    assert req.state == "failed"
    assert services.report_result(event, "callback", "nope", "answered") is False
    assert services.report_result(event, "bogus", req.pk, "answered") is False
