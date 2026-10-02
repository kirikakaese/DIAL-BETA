"""REST API: callbacks, scheduled calls, test ringback, PBX result hook."""
import datetime as dt

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.callback import services
from apps.callback.models import CallbackRequest, ScheduledCall
from apps.pbx.api import hook_secret

pytestmark = pytest.mark.django_db


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_create_list_cancel_callback(event, user, other_user, orga, ext_alice, ext_bob):
    c = _client(user)
    r = c.post("/api/v1/callback/requests/", {"event": "demo", "requester_number": "4242", "target_number": "4300"})
    assert r.status_code == 201, r.content
    pk = r.json()["id"]
    assert r.json()["state"] == "pending" and r.json()["kind"] == "ccbs"

    # duplicate -> 400
    r = c.post("/api/v1/callback/requests/", {"event": "demo", "requester_number": "4242", "target_number": "4300"})
    assert r.status_code == 400

    # not my extension -> 400
    r = c.post("/api/v1/callback/requests/", {"event": "demo", "requester_number": "4300", "target_number": "4242"})
    assert r.status_code == 400

    # visible to requester and to target owner, not to unrelated users
    assert _client(user).get("/api/v1/callback/requests/").json()["count"] == 1
    assert _client(other_user).get("/api/v1/callback/requests/").json()["count"] == 1
    assert _client(orga).get("/api/v1/callback/requests/").json()["count"] == 0
    assert _client(orga).get("/api/v1/callback/requests/?event=demo").json()["count"] == 1

    r = c.post(f"/api/v1/callback/requests/{pk}/cancel/")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert CallbackRequest.objects.get(pk=pk).state == "cancelled"


def test_scheduled_calls_crud(event, user, other_user, ext_alice):
    c = _client(user)
    when = (timezone.now() + dt.timedelta(hours=3)).isoformat()
    r = c.post("/api/v1/callback/scheduled-calls/", {"event": "demo", "extension": str(ext_alice.pk),
                                                    "scheduled_for": when, "repeat": "once"})
    assert r.status_code == 201, r.content
    pk = r.json()["id"]
    assert r.json()["extension_number"] == "4242" and r.json()["state"] == "scheduled"

    # someone else's extension -> 400
    r = _client(other_user).post("/api/v1/callback/scheduled-calls/",
                                 {"event": "demo", "extension": str(ext_alice.pk), "scheduled_for": when})
    assert r.status_code == 400
    assert _client(other_user).get("/api/v1/callback/scheduled-calls/").json()["count"] == 0

    # past -> 400
    r = c.post("/api/v1/callback/scheduled-calls/",
               {"event": "demo", "extension": str(ext_alice.pk),
                "scheduled_for": (timezone.now() - dt.timedelta(days=1)).isoformat()})
    assert r.status_code == 400

    r = c.patch(f"/api/v1/callback/scheduled-calls/{pk}/", {"snooze_minutes": 15})
    assert r.status_code == 200 and r.json()["snooze_minutes"] == 15

    r = c.post(f"/api/v1/callback/scheduled-calls/{pk}/snooze/", {"minutes": 4})
    assert r.status_code == 200 and r.json()["last_result"] == "snoozed"

    r = c.post(f"/api/v1/callback/scheduled-calls/{pk}/cancel/")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert ScheduledCall.objects.get(pk=pk).state == "cancelled"

    r = c.delete(f"/api/v1/callback/scheduled-calls/{pk}/")
    assert r.status_code == 204 and ScheduledCall.objects.filter(pk=pk).exists()


def test_test_ringback_endpoint(event, user, other_user, ext_alice, pbx):
    r = _client(other_user).post("/api/v1/callback/test-ringback/", {"event": "demo", "extension": "4242"})
    assert r.status_code == 403
    r = _client(user).post("/api/v1/callback/test-ringback/", {"event": "demo", "extension": "9876"})
    assert r.status_code == 404
    r = _client(user).post("/api/v1/callback/test-ringback/", {"event": "demo", "extension": "4242", "delay": 2})
    assert r.status_code == 201 and r.json()["delay_seconds"] == 2
    assert pbx.originated[-1]["destination"] == "4242"


def test_pbx_result_hook(client, event, user, ext_alice, pbx):
    call = services.schedule_wakeup(event, user, ext_alice, timezone.now() + dt.timedelta(minutes=1))
    services.fire_scheduled_call(call)
    hdr = {"HTTP_X_PET_PBX_SECRET": hook_secret()}
    r = client.post("/api/v1/callback/result/", {"event": "demo", "kind": "wakeup", "id": call.pk,
                                                  "result": "answered"})
    assert r.status_code == 401
    r = client.post("/api/v1/callback/result/", {"event": "demo", "kind": "wakeup", "id": call.pk,
                                                  "result": "answered"}, **hdr)
    assert r.status_code == 200 and r.json()["handled"] is True
    call.refresh_from_db()
    assert call.state == "answered"
    r = client.post("/api/v1/callback/result/", {"event": "nope", "kind": "wakeup", "id": 1}, **hdr)
    assert r.status_code == 400


def test_feature_code_hook_end_to_end(client, event, ext_alice, ext_bob, pbx):
    """The real ``apps/pbx/api.py`` hook dispatches into our services."""
    hdr = {"HTTP_X_PET_PBX_SECRET": hook_secret()}
    r = client.post("/api/v1/pbx/hooks/feature-code/", {"event": "demo", "caller": "4242", "code": "*66",
                                                        "target": "4300"}, **hdr)
    assert r.json()["handled"] is True
    r = client.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4300"}, **hdr)
    assert r.json() == {"handled": True, "result": 1}
    assert pbx.originated[-1]["destination"] == "4242"
    r = client.post("/api/v1/pbx/hooks/feature-code/", {"event": "demo", "caller": "4242", "code": "*99"}, **hdr)
    assert r.json()["handled"] is False
