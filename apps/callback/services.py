"""Callback services: CCBS/CCNR, test ringback and scheduled (wake-up) calls.

PBX contract (see ``docs/DEVELOPING.md`` and ``deploy/asterisk/conf/extensions.conf``):

``handle_feature_code(event, caller, code, target) -> bool``
    Called by the ``feature-code`` hook. ``code`` is the plan's feature code (``*66``/``*86``) or
    one of the service codes ``ringback`` / ``wakeup``. Additionally the dialplan may report call
    results through the same hook with ``code`` in ``callback-answered``, ``callback-failed``,
    ``wakeup-answered``, ``wakeup-failed``, ``ringback-answered``, ``ringback-failed`` and
    ``target`` = the ``PET_*_ID`` channel variable set at originate time.

``on_extension_idle(event, number) -> int``
    Called by the ``extension-idle`` hook from the hangup handler for *both* parties of a call.
    Fires every open callback whose *target* is ``number``. For CCBS that is the busy party becoming
    free; for CCNR Asterisk signals idle after the target's next call ends (i.e. the target has been
    active again), which is exactly when a "no reply" callback should be attempted.

Originates carry ``PET_SERVICE`` (dialplan exten in ``pet-services``: ``callback``, ``ringback``,
``wakeup-call``) plus ``PET_CALLBACK_ID``/``PET_CALLBACK_TARGET``, ``PET_RINGBACK_ID`` or
``PET_WAKEUP_ID``/``PET_ANNOUNCEMENT``.
"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.audit import log as audit
from apps.events.webhooks import emit
from apps.extensions.models import Extension
from apps.extensions.services import get_plan
from apps.pbx import get_pbx
from apps.pbx.base import PBXError

from .models import CallbackRequest, ScheduledCall, TestRingback

log = logging.getLogger("pet.callback")

CALLBACK_RETRY_MINUTES = 1
RESULT_CODES = {
    "callback-answered": ("callback", "answered"), "callback-failed": ("callback", "failed"),
    "wakeup-answered": ("wakeup", "answered"), "wakeup-failed": ("wakeup", "failed"),
    "ringback-answered": ("ringback", "answered"), "ringback-failed": ("ringback", "failed"),
}


class CallbackError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def _now(now=None):
    return now or timezone.now()


def event_tz(event) -> ZoneInfo:
    try:
        return ZoneInfo(event.timezone or settings.TIME_ZONE)
    except Exception:  # noqa: BLE001 - bad tz string on the event
        return ZoneInfo(settings.TIME_ZONE)


def active_extension(event, number: str) -> Extension | None:
    number = (number or "").strip()
    if not number:
        return None
    return Extension.objects.filter(event=event, number=number).active().select_related("owner").first()


def _owns(user, ext: Extension) -> bool:
    return user is not None and (ext.owner_id == user.pk or user.is_orga(ext.event))


def _cb_payload(req: CallbackRequest) -> dict:
    return {
        "id": req.pk, "event": req.event.slug, "kind": req.kind, "state": req.state,
        "requester": req.requester_number, "target": req.target_number, "attempts": req.attempts,
        "channel_id": req.channel_id,
    }


def _sc_payload(call: ScheduledCall) -> dict:
    return {
        "id": call.pk, "event": call.event.slug, "extension": call.extension.number, "state": call.state,
        "scheduled_for": call.scheduled_for.isoformat(), "repeat": call.repeat, "attempts": call.attempts,
        "last_result": call.last_result, "owner": call.owner.username if call.owner else None,
    }


# --------------------------------------------------------------------------- CCBS / CCNR

def request_callback(event, requester_number: str, target_number: str, kind: str = CallbackRequest.Kind.CCBS,
                     *, via: str = "feature_code", user=None, request=None, ttl_minutes: int | None = None,
                     note: str = "") -> CallbackRequest:
    """Create a pending CCBS/CCNR request from ``requester_number`` towards ``target_number``."""
    if kind not in CallbackRequest.Kind.values:
        raise CallbackError(_("Unknown callback kind."))
    requester = active_extension(event, requester_number)
    if requester is None:
        raise CallbackError(_("Your extension is not active in this event."))
    target = active_extension(event, target_number)
    if target is None:
        raise CallbackError(_("The target extension does not exist or is not active."))
    if requester.pk == target.pk:
        raise CallbackError(_("You cannot request a callback from yourself."))
    if not target.allow_callback:
        raise CallbackError(_("This extension does not accept callbacks."))
    if via == "web" and not _owns(user, requester):
        raise CallbackError(_("You may only request callbacks for your own extensions."))
    if CallbackRequest.objects.filter(event=event, requester=requester, target=target,
                                      state__in=CallbackRequest.OPEN_STATES).exists():
        raise CallbackError(_("A callback to this extension is already pending."))

    ttl = ttl_minutes or getattr(settings, "PET_CALLBACK_DEFAULT_TTL_MINUTES", 30)
    req = CallbackRequest.objects.create(
        event=event, kind=kind, requester=requester, target=target,
        requester_number=requester.number, target_number=target.number,
        expires_at=timezone.now() + dt.timedelta(minutes=ttl), note=note[:200],
    )
    audit(action="create", actor=user, target=req, event=event, request=request,
          message=f"{kind.upper()} requested via {via}: {requester.number} -> {target.number}")
    emit("callback.requested", _cb_payload(req), event=event)
    return req


def cancel(req: CallbackRequest, user=None, request=None, reason: str = "cancelled") -> CallbackRequest:
    if not req.is_open:
        return req
    req.state = CallbackRequest.State.CANCELLED
    req.note = (req.note or reason)[:200]
    req.save(update_fields=["state", "note"])
    audit(action="delete", actor=user, target=req, event=req.event, request=request,
          message=f"Callback cancelled: {req.requester_number} -> {req.target_number}")
    emit("callback.cancelled", _cb_payload(req), event=req.event)
    return req


def cancel_callback(event, requester_number: str, user=None, request=None) -> int:
    """Cancel every open callback requested from ``requester_number``. Returns the count."""
    qs = CallbackRequest.objects.filter(event=event, requester_number=(requester_number or "").strip(),
                                        state__in=CallbackRequest.OPEN_STATES)
    n = 0
    for req in qs.select_related("event"):
        cancel(req, user=user, request=request, reason="cancelled via feature code")
        n += 1
    return n


def deliver_callback(req: CallbackRequest, *, now=None) -> bool:
    """Originate the callback leg to the requester; the dialplan bridges to the target.

    Returns True if the originate was accepted. On ``PBXError`` the request goes back to ``pending``
    with a retry in one minute, or to ``failed`` after ``MAX_ATTEMPTS``.
    """
    now = _now(now)
    if req.expires_at <= now:
        req.state = CallbackRequest.State.EXPIRED
        req.save(update_fields=["state"])
        return False
    req.state = CallbackRequest.State.DIALING
    req.attempts += 1
    req.last_attempt_at = now
    req.next_attempt_at = None
    req.save(update_fields=["state", "attempts", "last_attempt_at", "next_attempt_at"])
    try:
        cid = get_pbx(req.event).originate(
            event=req.event, destination=req.requester_number, caller_id=f"Callback {req.target_number}",
            context="pet-services",
            variables={"PET_SERVICE": "callback", "PET_CALLBACK_TARGET": req.target_number,
                       "PET_CALLBACK_ID": str(req.pk), "PET_CALLBACK_KIND": req.kind},
        )
    except PBXError as exc:
        log.warning("callback %s originate failed (attempt %s): %s", req.pk, req.attempts, exc)
        req.note = str(exc)[:200]
        if req.attempts >= CallbackRequest.MAX_ATTEMPTS:
            req.state = CallbackRequest.State.FAILED
            emit("callback.failed", _cb_payload(req), event=req.event)
        else:
            req.state = CallbackRequest.State.PENDING
            req.next_attempt_at = now + dt.timedelta(minutes=CALLBACK_RETRY_MINUTES)
        req.save(update_fields=["state", "note", "next_attempt_at"])
        return False
    req.channel_id = cid or ""
    req.state = CallbackRequest.State.COMPLETED
    req.save(update_fields=["channel_id", "state"])
    emit("callback.completed", _cb_payload(req), event=req.event)
    return True


def on_extension_idle(event, number: str, *, now=None) -> int:
    """``extension-idle`` hook: fire open callbacks targeting ``number``. Returns the count fired."""
    number = (number or "").strip()
    if not number:
        return 0
    now = _now(now)
    qs = (CallbackRequest.objects.filter(event=event, target_number=number, state=CallbackRequest.State.PENDING)
          .select_related("event").order_by("created_at"))
    fired = 0
    for req in qs:
        if req.expires_at <= now:
            req.state = CallbackRequest.State.EXPIRED
            req.save(update_fields=["state"])
            continue
        if deliver_callback(req, now=now):
            fired += 1
    return fired


def expire_stale(now=None) -> int:
    now = _now(now)
    qs = CallbackRequest.objects.filter(state=CallbackRequest.State.PENDING, expires_at__lte=now)
    ids = list(qs.values_list("pk", flat=True))
    n = qs.update(state=CallbackRequest.State.EXPIRED)
    for req in CallbackRequest.objects.filter(pk__in=ids).select_related("event"):
        emit("callback.expired", _cb_payload(req), event=req.event)
    return n


# --------------------------------------------------------------------------- test ringback

def request_test_ringback(event, caller_number: str, delay: int | None = None, *, user=None,
                          request=None) -> TestRingback:
    caller_number = (caller_number or "").strip()
    if not caller_number:
        raise CallbackError(_("Caller number is required."))
    ext = active_extension(event, caller_number)
    if user is not None and ext is not None and not _owns(user, ext):
        raise CallbackError(_("You may only request a ringback to your own extensions."))
    if delay is None:
        delay = getattr(settings, "PET_TEST_RINGBACK_DELAY_SECONDS", 10)
    delay = max(0, min(int(delay), 600))
    rb = TestRingback.objects.create(event=event, extension=ext, caller_number=caller_number, delay_seconds=delay,
                                     next_attempt_at=timezone.now() + dt.timedelta(seconds=delay))
    audit(action="create", actor=user, target=rb, event=event, request=request,
          message=f"Test ringback to {caller_number} in {delay}s")
    from .tasks import fire_test_ringback

    fire_test_ringback.apply_async(args=[rb.pk], countdown=delay)  # countdown is ignored in eager mode
    return rb


def fire_test_ringback(rb: TestRingback) -> bool:
    if rb.state not in (TestRingback.State.SCHEDULED, TestRingback.State.FAILED):
        return False
    rb.state = TestRingback.State.DIALING
    rb.save(update_fields=["state"])
    try:
        cid = get_pbx(rb.event).originate(
            event=rb.event, destination=rb.caller_number, caller_id="Ringback test", context="pet-services",
            variables={"PET_SERVICE": "ringback", "PET_RINGBACK_ID": str(rb.pk)},
        )
    except PBXError as exc:
        log.warning("ringback %s originate failed: %s", rb.pk, exc)
        rb.state = TestRingback.State.FAILED
        rb.save(update_fields=["state"])
        return False
    rb.channel_id = cid or ""
    rb.state = TestRingback.State.DELIVERED
    rb.save(update_fields=["channel_id", "state"])
    return True


# --------------------------------------------------------------------------- scheduled / wake-up calls

def parse_wakeup_time(event, hhmm: str, *, now=None) -> dt.datetime:
    """``HHMM`` (event-local) -> next matching aware datetime (today if still ahead, else tomorrow)."""
    digits = "".join(ch for ch in str(hhmm or "") if ch.isdigit())
    if len(digits) not in (3, 4):
        raise CallbackError(_("Time must be given as HHMM."))
    digits = digits.zfill(4)
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour > 23 or minute > 59:
        raise CallbackError(_("Invalid time."))
    tz = event_tz(event)
    local_now = _now(now).astimezone(tz)
    when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= local_now:
        when += dt.timedelta(days=1)
    return when


def schedule_wakeup(event, user, extension: Extension, when: dt.datetime, *, repeat: str = ScheduledCall.Repeat.ONCE,
                    announcement: str = ScheduledCall.Announcement.DEFAULT, announcement_text: str = "",
                    announcement_file=None, max_retries: int = 3, retry_interval_minutes: int = 5,
                    snooze_minutes: int = 9, request=None, now=None, check_owner: bool = True) -> ScheduledCall:
    if extension.event_id != event.pk or not extension.is_active:
        raise CallbackError(_("The extension is not active in this event."))
    if check_owner and user is not None and not _owns(user, extension):
        raise CallbackError(_("You may only schedule calls to your own extensions."))
    if timezone.is_naive(when):
        when = timezone.make_aware(when, event_tz(event))
    if when <= _now(now) - dt.timedelta(minutes=1):
        raise CallbackError(_("The scheduled time is in the past."))
    if repeat not in ScheduledCall.Repeat.values:
        raise CallbackError(_("Unknown repeat mode."))
    if announcement not in ScheduledCall.Announcement.values:
        raise CallbackError(_("Unknown announcement type."))
    call = ScheduledCall(
        event=event, owner=user, extension=extension, scheduled_for=when, next_attempt_at=when, repeat=repeat,
        announcement=announcement, announcement_text=(announcement_text or "")[:300],
        max_retries=max_retries, retry_interval_minutes=max(1, retry_interval_minutes),
        snooze_minutes=max(1, snooze_minutes),
    )
    if announcement_file:
        call.announcement_file = announcement_file
    call.save()
    audit(action="create", actor=user, target=call, event=event, request=request,
          message=f"Scheduled {repeat} call to {extension.number} at {when.isoformat()}")
    emit("scheduled_call.created", _sc_payload(call), event=event)
    return call


def cancel_scheduled(call: ScheduledCall, user=None, request=None) -> ScheduledCall:
    if not call.is_open:
        return call
    if call.state == ScheduledCall.State.DIALING and call.channel_id:
        try:
            get_pbx(call.event).hangup(call.channel_id)
        except PBXError:  # pragma: no cover - best effort
            pass
    call.state = ScheduledCall.State.CANCELLED
    call.next_attempt_at = None
    call.save(update_fields=["state", "next_attempt_at", "updated_at"])
    audit(action="delete", actor=user, target=call, event=call.event, request=request,
          message=f"Scheduled call to {call.extension.number} cancelled")
    emit("scheduled_call.cancelled", _sc_payload(call), event=call.event)
    return call


def snooze(call: ScheduledCall, minutes: int | None = None, *, now=None, user=None, request=None) -> ScheduledCall:
    """Ring again in ``minutes`` (default: the call's ``snooze_minutes``). Resets the retry counter."""
    if call.state == ScheduledCall.State.CANCELLED:
        raise CallbackError(_("This call was cancelled."))
    minutes = minutes or call.snooze_minutes
    call.state = ScheduledCall.State.SCHEDULED
    call.attempts = 0
    call.next_attempt_at = _now(now) + dt.timedelta(minutes=max(1, int(minutes)))
    call.last_result = "snoozed"
    call.save(update_fields=["state", "attempts", "next_attempt_at", "last_result", "updated_at"])
    audit(action="update", actor=user, target=call, event=call.event, request=request,
          message=f"Snoozed {minutes} min")
    return call


def due_scheduled_calls(now=None):
    return (ScheduledCall.objects.filter(state=ScheduledCall.State.SCHEDULED, next_attempt_at__lte=_now(now))
            .select_related("event", "extension", "owner").order_by("next_attempt_at"))


def _next_occurrence(call: ScheduledCall, now=None) -> ScheduledCall | None:
    """For ``repeat=daily``: clone the call for the same local time on the next day."""
    if call.repeat != ScheduledCall.Repeat.DAILY:
        return None
    tz = event_tz(call.event)
    local = call.scheduled_for.astimezone(tz)
    now_local = _now(now).astimezone(tz)
    nxt = local + dt.timedelta(days=1)
    while nxt <= now_local:
        nxt += dt.timedelta(days=1)
    nxt = nxt.replace(tzinfo=None).replace(tzinfo=tz)  # keep the wall-clock time across DST changes
    clone = ScheduledCall.objects.create(
        event=call.event, owner=call.owner, extension=call.extension, scheduled_for=nxt, next_attempt_at=nxt,
        repeat=call.repeat, max_retries=call.max_retries, retry_interval_minutes=call.retry_interval_minutes,
        announcement=call.announcement, announcement_text=call.announcement_text,
        announcement_file=call.announcement_file or None, snooze_minutes=call.snooze_minutes,
    )
    return clone


def _scheduled_attempt_failed(call: ScheduledCall, reason: str, *, now=None) -> None:
    now = _now(now)
    call.last_result = reason[:200]
    call.channel_id = ""
    if call.attempts <= call.max_retries:
        call.state = ScheduledCall.State.SCHEDULED
        call.next_attempt_at = now + dt.timedelta(minutes=call.retry_interval_minutes)
    else:
        call.state = ScheduledCall.State.FAILED
        call.next_attempt_at = None
    call.save(update_fields=["state", "last_result", "channel_id", "next_attempt_at", "updated_at"])
    if call.state == ScheduledCall.State.FAILED:
        emit("scheduled_call.failed", _sc_payload(call), event=call.event)
        _next_occurrence(call, now)


def fire_scheduled_call(call: ScheduledCall, *, now=None) -> bool:
    """Originate the wake-up call. Returns True if the PBX accepted the originate.

    After a successful originate the call stays ``dialing`` until the dialplan reports
    ``wakeup-answered`` (→ ``answered``); if no result arrives within ``retry_interval_minutes`` the
    dispatcher treats it as unanswered and retries (up to ``max_retries``).
    """
    if call.state not in ScheduledCall.OPEN_STATES:
        return False
    now = _now(now)
    call.state = ScheduledCall.State.DIALING
    call.attempts += 1
    call.save(update_fields=["state", "attempts", "updated_at"])
    try:
        cid = get_pbx(call.event).originate(
            event=call.event, destination=call.extension.number, caller_id="Wake-up call", context="pet-services",
            variables={"PET_SERVICE": "wakeup-call", "PET_WAKEUP_ID": str(call.pk),
                       "PET_ANNOUNCEMENT": call.announcement_value},
            timeout=45,
        )
    except PBXError as exc:
        log.warning("wake-up %s originate failed (attempt %s): %s", call.pk, call.attempts, exc)
        _scheduled_attempt_failed(call, f"originate failed: {exc}", now=now)
        return False
    call.channel_id = cid or ""
    call.next_attempt_at = now + dt.timedelta(minutes=call.retry_interval_minutes)  # answer deadline
    call.last_result = "dialing"
    call.save(update_fields=["channel_id", "next_attempt_at", "last_result", "updated_at"])
    return True


def _scheduled_answered(call: ScheduledCall, *, now=None) -> None:
    call.state = ScheduledCall.State.ANSWERED
    call.last_result = "answered"
    call.next_attempt_at = None
    call.save(update_fields=["state", "last_result", "next_attempt_at", "updated_at"])
    emit("scheduled_call.answered", _sc_payload(call), event=call.event)
    _next_occurrence(call, now)


def dispatch_due(now=None) -> dict:
    """Beat entry point: fire due scheduled calls, time out unanswered ones, retry callbacks."""
    now = _now(now)
    out = {"scheduled_fired": 0, "scheduled_timeouts": 0, "callbacks_retried": 0}
    for call in list(due_scheduled_calls(now)):
        fire_scheduled_call(call, now=now)
        out["scheduled_fired"] += 1
    for call in list(ScheduledCall.objects.filter(state=ScheduledCall.State.DIALING, next_attempt_at__lte=now)
                     .select_related("event", "extension")):
        _scheduled_attempt_failed(call, "no answer", now=now)
        out["scheduled_timeouts"] += 1
    for req in list(CallbackRequest.objects.filter(state=CallbackRequest.State.PENDING, next_attempt_at__lte=now)
                    .select_related("event")):
        deliver_callback(req, now=now)
        out["callbacks_retried"] += 1
    return out


# --------------------------------------------------------------------------- results from the PBX

def report_result(event, kind: str, pk, result: str, *, now=None) -> bool:
    """Record the outcome of an originated call.

    ``kind`` is ``callback`` / ``wakeup`` / ``ringback`` (the ``PET_SERVICE`` used at originate),
    ``pk`` the id from the ``PET_*_ID`` channel variable, ``result`` ``answered`` or ``failed``.
    Reachable via the ``feature-code`` hook (``code=<kind>-<result>&target=<pk>``) or
    ``POST /api/v1/callback/result/`` with the PBX secret.
    """
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return False
    answered = result == "answered"
    if kind == "wakeup":
        call = ScheduledCall.objects.filter(event=event, pk=pk).select_related("event", "extension").first()
        if call is None or call.state != ScheduledCall.State.DIALING:
            return False
        if answered:
            _scheduled_answered(call, now=now)
        else:
            _scheduled_attempt_failed(call, "failed", now=now)
        return True
    if kind == "callback":
        req = CallbackRequest.objects.filter(event=event, pk=pk).select_related("event").first()
        if req is None:
            return False
        if not answered and req.state in (CallbackRequest.State.DIALING, CallbackRequest.State.COMPLETED):
            req.state = CallbackRequest.State.FAILED
            req.note = "failed"
            req.save(update_fields=["state", "note"])
            emit("callback.failed", _cb_payload(req), event=req.event)
        elif answered and req.state != CallbackRequest.State.COMPLETED:
            req.state = CallbackRequest.State.COMPLETED
            req.save(update_fields=["state"])
            emit("callback.completed", _cb_payload(req), event=req.event)
        return True
    if kind == "ringback":
        rb = TestRingback.objects.filter(event=event, pk=pk).first()
        if rb is None:
            return False
        rb.state = TestRingback.State.DELIVERED if answered else TestRingback.State.FAILED
        rb.save(update_fields=["state"])
        return True
    return False


def mark_answered(kind: str, pk, event=None) -> bool:
    event = event or _event_of(kind, pk)
    return event is not None and report_result(event, kind, pk, "answered")


def mark_failed(kind: str, pk, event=None) -> bool:
    event = event or _event_of(kind, pk)
    return event is not None and report_result(event, kind, pk, "failed")


def _event_of(kind, pk):
    model = {"wakeup": ScheduledCall, "callback": CallbackRequest, "ringback": TestRingback}.get(kind)
    obj = model.objects.filter(pk=pk).select_related("event").first() if model else None
    return obj.event if obj else None


# --------------------------------------------------------------------------- feature codes

def handle_feature_code(event, caller: str, code: str, target: str) -> bool:
    """``feature-code`` hook. Returns True when this app handled ``code``."""
    caller = (caller or "").strip()
    code = (code or "").strip()
    target = (target or "").strip()
    if not code:
        return False
    plan = get_plan(event)
    try:
        if code in RESULT_CODES:
            kind, result = RESULT_CODES[code]
            return report_result(event, kind, target, result)
        if code in (plan.callback_request_code, "callback", "ccbs", "ccnr"):
            if not target:
                return False
            kind = CallbackRequest.Kind.CCNR if code == "ccnr" else CallbackRequest.Kind.CCBS
            request_callback(event, caller, target, kind, via="feature_code")
            return True
        if code in (plan.callback_cancel_code, "callback-cancel"):
            cancel_callback(event, caller)
            return True
        if code == "ringback":
            request_test_ringback(event, caller or target)
            return True
        if code == "wakeup":
            ext = active_extension(event, caller)
            if ext is None:
                return False
            when = parse_wakeup_time(event, target)
            schedule_wakeup(event, ext.owner, ext, when, check_owner=False)
            return True
    except CallbackError as exc:
        log.info("feature code %s from %s rejected: %s", code, caller, exc)
        return False
    return False
