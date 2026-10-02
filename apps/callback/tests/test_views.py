"""Portal views: owner index, forms, cancel/snooze, orga overview."""
import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.callback import services
from apps.callback.models import CallbackRequest, ScheduledCall

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.callback.tests.urls_stub")]


def _url(name, event, *args):
    return reverse(f"callback:{name}", args=[event.slug, *args])


def test_index_requires_login(client, event):
    r = client.get(_url("index", event))
    assert r.status_code == 302 and "login" in r["Location"]


def test_index_for_owner(client, event, user, member, ext_alice, ext_bob):
    services.request_callback(event, "4242", "4300", "ccbs")
    services.schedule_wakeup(event, user, ext_alice, timezone.now() + dt.timedelta(hours=1))
    client.force_login(user)
    r = client.get(_url("index", event))
    assert r.status_code == 200
    body = r.content.decode()
    assert "4300" in body and "*66" in body and "9000" in body and "9001" in body
    assert "Snooze" in body and "Call me back" in body


def test_index_without_extension(client, event, user, member):
    client.force_login(user)
    r = client.get(_url("index", event))
    assert r.status_code == 200 and "no active extension" in r.content.decode()


def test_request_ringback_and_wakeup_forms(client, event, user, member, ext_alice, ext_bob, pbx):
    client.force_login(user)
    r = client.post(_url("request", event), {"requester": str(ext_alice.pk), "target_number": "4300", "kind": "ccbs"})
    assert r.status_code == 302
    assert CallbackRequest.objects.filter(requester=ext_alice, target=ext_bob, state="pending").exists()

    r = client.post(_url("ringback", event), {"extension": str(ext_alice.pk), "delay": 3})
    assert r.status_code == 302
    assert pbx.originated[-1]["destination"] == "4242" and pbx.originated[-1]["variables"]["PET_SERVICE"] == "ringback"

    tomorrow = (timezone.now() + dt.timedelta(days=1)).strftime("%Y-%m-%dT07:30")
    r = client.post(_url("wakeup_new", event), {
        "extension": str(ext_alice.pk), "scheduled_for": tomorrow, "repeat": "daily", "announcement": "custom",
        "announcement_text": "Good morning", "max_retries": 2, "retry_interval_minutes": 5, "snooze_minutes": 9,
    })
    assert r.status_code == 302, r.content.decode()[:2000]
    call = ScheduledCall.objects.get()
    local = timezone.localtime(call.scheduled_for, services.event_tz(event))
    assert (local.hour, local.minute) == (7, 30) and call.repeat == "daily" and call.announcement_text == "Good morning"

    # custom announcement without text/file -> form error re-rendered
    r = client.post(_url("wakeup_new", event), {
        "extension": str(ext_alice.pk), "scheduled_for": tomorrow, "repeat": "once", "announcement": "custom",
        "max_retries": 2, "retry_interval_minutes": 5, "snooze_minutes": 9,
    })
    assert r.status_code == 200 and "custom announcement" in r.content.decode()


def test_cancel_snooze_and_permissions(client, event, user, other_user, member, ext_alice, ext_bob):
    req = services.request_callback(event, "4242", "4300", "ccbs")
    call = services.schedule_wakeup(event, user, ext_alice, timezone.now() + dt.timedelta(hours=1))

    # a third user (not owner, not orga) may not touch them
    from apps.accounts.models import User

    stranger = User.objects.create_user(email="c@example.org", username="carol", password="pw-carol-1234")
    client.force_login(stranger)
    assert client.post(_url("cancel", event, req.pk)).status_code == 403
    assert client.post(_url("wakeup_cancel", event, call.pk)).status_code == 403

    # the *target* owner may cancel a callback aimed at them
    client.force_login(other_user)
    assert client.post(_url("cancel", event, req.pk)).status_code == 302
    req.refresh_from_db()
    assert req.state == "cancelled"

    client.force_login(user)
    assert client.post(_url("wakeup_snooze", event, call.pk)).status_code == 302
    call.refresh_from_db()
    assert call.last_result == "snoozed"
    assert client.post(_url("wakeup_cancel", event, call.pk)).status_code == 302
    call.refresh_from_db()
    assert call.state == "cancelled"


def test_orga_all_and_fire_now(client, event, user, orga, ext_alice, ext_bob, pbx):
    req = services.request_callback(event, "4242", "4300", "ccbs")
    call = services.schedule_wakeup(event, user, ext_alice, timezone.now() + dt.timedelta(hours=1))
    rb = services.request_test_ringback(event, "4242", 0)
    pbx.reset()

    client.force_login(user)
    assert client.get(_url("all", event)).status_code == 403

    client.force_login(orga)
    r = client.get(_url("all", event))
    assert r.status_code == 200 and "Fire now" in r.content.decode()
    assert client.get(_url("all", event) + "?state=open").status_code == 200

    assert client.post(_url("fire_now", event, "callback", req.pk)).status_code == 302
    req.refresh_from_db()
    assert req.state == "completed" and pbx.originated[-1]["destination"] == "4242"
    assert client.post(_url("fire_now", event, "wakeup", call.pk)).status_code == 302
    call.refresh_from_db()
    assert call.state == "dialing" and pbx.originated[-1]["variables"]["PET_SERVICE"] == "wakeup-call"
    assert client.post(_url("fire_now", event, "ringback", rb.pk)).status_code == 302
    assert pbx.originated[-1]["variables"]["PET_SERVICE"] == "ringback"
    assert len(pbx.originated) == 3
