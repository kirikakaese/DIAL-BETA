"""Test ringback and scheduled / wake-up calls."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.callback import models as m
from apps.callback import services, tasks
from apps.callback.models import ScheduledCall
from apps.callback.services import CallbackError
from apps.pbx.base import PBXError

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- ringback

def test_ringback_feature_code_originates(event, ext_alice, pbx):
    assert services.handle_feature_code(event, "4242", "ringback", "4242") is True
    rb = m.TestRingback.objects.get()
    assert rb.extension == ext_alice and rb.delay_seconds == 10
    # celery is eager: the fire task already ran
    assert rb.state == "delivered" and rb.channel_id == pbx.originated[-1]["id"]
    o = pbx.originated[-1]
    assert o["destination"] == "4242" and o["variables"]["DIAL_SERVICE"] == "ringback"
    assert o["variables"]["DIAL_RINGBACK_ID"] == str(rb.pk)


def test_ringback_web_owner_check(event, user, other_user, ext_alice, pbx):
    with pytest.raises(CallbackError):
        services.request_test_ringback(event, "4242", 5, user=other_user)
    rb = services.request_test_ringback(event, "4242", 5, user=user)
    assert rb.delay_seconds == 5 and len(pbx.originated) == 1


def test_ringback_pbx_error(event, ext_alice, pbx, monkeypatch):
    def boom(**kw):
        raise PBXError("down")

    monkeypatch.setattr(pbx, "originate", boom)
    rb = services.request_test_ringback(event, "4242")
    rb.refresh_from_db()
    assert rb.state == "failed"
    assert tasks.fire_test_ringback(99999) is False


# --------------------------------------------------------------------------- wake-up

def test_parse_wakeup_time_today_or_tomorrow(event):
    tz = ZoneInfo(event.timezone)
    now = dt.datetime(2026, 9, 13, 8, 0, tzinfo=tz)
    when = services.parse_wakeup_time(event, "0930", now=now)
    assert when == dt.datetime(2026, 9, 13, 9, 30, tzinfo=tz)
    when = services.parse_wakeup_time(event, "0700", now=now)
    assert when == dt.datetime(2026, 9, 14, 7, 0, tzinfo=tz)
    with pytest.raises(CallbackError):
        services.parse_wakeup_time(event, "2560")
    with pytest.raises(CallbackError):
        services.parse_wakeup_time(event, "12")


def test_wakeup_feature_code(event, user, ext_alice, pbx):
    assert services.handle_feature_code(event, "4242", "wakeup", "0730") is True
    call = ScheduledCall.objects.get()
    assert call.extension == ext_alice and call.owner == user and call.state == "scheduled"
    assert call.next_attempt_at == call.scheduled_for
    local = call.scheduled_for.astimezone(ZoneInfo(event.timezone))
    assert (local.hour, local.minute) == (7, 30)
    assert services.handle_feature_code(event, "4242", "wakeup", "abcd") is False
    assert services.handle_feature_code(event, "5555", "wakeup", "0730") is False  # unknown caller


def test_schedule_fire_answer_and_retry(event, user, ext_alice, pbx):
    now = timezone.now().replace(microsecond=0)
    when = now + dt.timedelta(hours=1)
    call = services.schedule_wakeup(event, user, ext_alice, when, max_retries=1, retry_interval_minutes=5)
    assert list(services.due_scheduled_calls(now)) == []
    assert list(services.due_scheduled_calls(when)) == [call]

    out = services.dispatch_due(now=when)
    assert out["scheduled_fired"] == 1
    call.refresh_from_db()
    o = pbx.originated[-1]
    assert o["destination"] == "4242" and o["variables"]["DIAL_SERVICE"] == "wakeup-call"
    assert o["variables"]["DIAL_WAKEUP_ID"] == str(call.pk) and o["variables"]["DIAL_ANNOUNCEMENT"] == ""
    assert call.state == "dialing" and call.attempts == 1 and call.channel_id == o["id"]
    assert call.next_attempt_at == when + dt.timedelta(minutes=5)

    # no answer within the interval -> retry
    out = services.dispatch_due(now=when + dt.timedelta(minutes=5))
    assert out["scheduled_timeouts"] == 1
    call.refresh_from_db()
    assert call.state == "scheduled" and call.last_result == "no answer"
    assert call.next_attempt_at == when + dt.timedelta(minutes=10)

    out = services.dispatch_due(now=when + dt.timedelta(minutes=10))
    assert out["scheduled_fired"] == 1 and len(pbx.originated) == 2
    call.refresh_from_db()
    assert call.attempts == 2 and call.state == "dialing"

    # answered via the feature-code result hook
    assert services.handle_feature_code(event, "4242", "wakeup-answered", str(call.pk)) is True
    call.refresh_from_db()
    assert call.state == "answered" and call.next_attempt_at is None
    assert ScheduledCall.objects.count() == 1  # once: no next occurrence


def test_retries_exhausted_then_failed(event, user, ext_alice, pbx):
    now = timezone.now()
    call = services.schedule_wakeup(event, user, ext_alice, now + dt.timedelta(minutes=1), max_retries=1,
                                    retry_interval_minutes=1)
    t = now + dt.timedelta(minutes=1)
    for _ in range(2):
        services.dispatch_due(now=t)  # fire
        t += dt.timedelta(minutes=1)
        services.dispatch_due(now=t)  # answer deadline passed -> timeout, retry scheduled
        t += dt.timedelta(minutes=1)
    call.refresh_from_db()
    assert call.attempts == 2 and call.state == "failed" and len(pbx.originated) == 2


def test_originate_error_uses_retry_interval(event, user, ext_alice, pbx, monkeypatch):
    def boom(**kw):
        raise PBXError("down")

    monkeypatch.setattr(pbx, "originate", boom)
    now = timezone.now()
    call = services.schedule_wakeup(event, user, ext_alice, now + dt.timedelta(minutes=2), retry_interval_minutes=3)
    assert services.fire_scheduled_call(call, now=call.scheduled_for) is False
    assert call.state == "scheduled" and "originate failed" in call.last_result
    assert call.next_attempt_at == call.scheduled_for + dt.timedelta(minutes=3)


def test_daily_repeat_creates_next_occurrence(event, user, ext_alice, pbx):
    tz = ZoneInfo(event.timezone)
    when = (timezone.now() + dt.timedelta(hours=2)).astimezone(tz).replace(second=0, microsecond=0)
    call = services.schedule_wakeup(event, user, ext_alice, when, repeat="daily", announcement="custom",
                                    announcement_text="Rise and shine")
    services.fire_scheduled_call(call, now=when)
    assert pbx.originated[-1]["variables"]["DIAL_ANNOUNCEMENT"] == "Rise and shine"
    services.report_result(event, "wakeup", call.pk, "answered", now=when)
    nxt = ScheduledCall.objects.exclude(pk=call.pk).get()
    assert nxt.state == "scheduled" and nxt.repeat == "daily" and nxt.announcement_text == "Rise and shine"
    local = nxt.scheduled_for.astimezone(tz)
    assert local.date() == (when + dt.timedelta(days=1)).date()
    assert (local.hour, local.minute) == (when.hour, when.minute)


def test_snooze_and_cancel(event, user, other_user, ext_alice, pbx):
    now = timezone.now()
    call = services.schedule_wakeup(event, user, ext_alice, now + dt.timedelta(minutes=5), snooze_minutes=9)
    services.fire_scheduled_call(call, now=call.scheduled_for)
    services.report_result(event, "wakeup", call.pk, "answered")
    services.snooze(call, now=now)
    assert call.state == "scheduled" and call.attempts == 0
    assert call.next_attempt_at == now + dt.timedelta(minutes=9)
    services.cancel_scheduled(call, user=user)
    assert call.state == "cancelled" and call.next_attempt_at is None
    with pytest.raises(CallbackError):
        services.snooze(call)
    with pytest.raises(CallbackError):
        services.schedule_wakeup(event, other_user, ext_alice, now + dt.timedelta(minutes=5))
    with pytest.raises(CallbackError):
        services.schedule_wakeup(event, user, ext_alice, now - dt.timedelta(hours=1))


def test_fire_scheduled_call_task(event, user, ext_alice, pbx):
    call = services.schedule_wakeup(event, user, ext_alice, timezone.now() + dt.timedelta(minutes=5))
    assert tasks.fire_scheduled_call(call.pk) is True
    assert tasks.fire_scheduled_call(424242) is False
    assert tasks.dispatch_due_callbacks()["scheduled_fired"] == 0
