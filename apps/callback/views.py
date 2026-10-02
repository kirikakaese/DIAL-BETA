"""Portal views: self-service callbacks / wake-up calls / test ringback, plus an orga overview."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.audit import log as audit
from apps.extensions.models import Extension
from apps.extensions.services import get_plan
from apps.portal.shortcuts import require_orga, with_event

from . import services
from .forms import CallbackRequestForm, TestRingbackForm, WakeupForm
from .models import CallbackRequest, ScheduledCall, TestRingback
from .services import CallbackError


def _index_url(event):
    return reverse("callback:index", args=[event.slug])


def _my_extensions(event, user):
    return Extension.objects.filter(event=event, owner=user).active().order_by("number")


def _my_callbacks(event, user):
    return (CallbackRequest.objects.filter(event=event)
            .filter(Q(requester__owner=user) | Q(target__owner=user))
            .select_related("requester", "target"))


def _my_scheduled(event, user):
    return (ScheduledCall.objects.filter(event=event).filter(Q(owner=user) | Q(extension__owner=user))
            .select_related("extension"))


def _visible_callback(request, event, pk):
    req = get_object_or_404(CallbackRequest.objects.select_related("requester", "target", "event"),
                            pk=pk, event=event)
    if not (request.user.is_orga(event) or req.requester.owner_id == request.user.pk
            or req.target.owner_id == request.user.pk):
        raise PermissionDenied
    return req


def _visible_scheduled(request, event, pk):
    call = get_object_or_404(ScheduledCall.objects.select_related("extension", "event", "owner"), pk=pk, event=event)
    if not (request.user.is_orga(event) or call.owner_id == request.user.pk
            or call.extension.owner_id == request.user.pk):
        raise PermissionDenied
    return call


@login_required
@with_event
def index(request, slug, *, event):
    exts = _my_extensions(event, request.user)
    plan = get_plan(event)
    ctx = {
        "event": event, "plan": plan, "my_extensions": exts, "active": "index",
        "pending": _my_callbacks(event, request.user).filter(state__in=CallbackRequest.OPEN_STATES),
        "history": _my_callbacks(event, request.user).exclude(state__in=CallbackRequest.OPEN_STATES)[:10],
        "scheduled": _my_scheduled(event, request.user).filter(state__in=ScheduledCall.OPEN_STATES),
        "scheduled_done": _my_scheduled(event, request.user).exclude(state__in=ScheduledCall.OPEN_STATES)
                          .order_by("-scheduled_for")[:10],
        "ringbacks": TestRingback.objects.filter(event=event, extension__owner=request.user)[:5],
        # distinct auto_id per form: three forms on one page each have an ``extension`` field
        "ringback_form": TestRingbackForm(extensions=exts, auto_id="ringback_%s"),
        "callback_form": CallbackRequestForm(extensions=exts, auto_id="callback_%s"),
        "wakeup_form": WakeupForm(extensions=exts, event=event, auto_id="wakeup_%s"),
        "now": timezone.now(),
    }
    return render(request, "callback/index.html", ctx)


@login_required
@with_event
@require_POST
def request_view(request, slug, *, event):
    form = CallbackRequestForm(request.POST, extensions=_my_extensions(event, request.user))
    if form.is_valid():
        try:
            req = services.request_callback(event, form.cleaned_data["requester"].number,
                                            form.cleaned_data["target_number"], form.cleaned_data["kind"],
                                            via="web", user=request.user, request=request)
            messages.success(request, _("Callback to %(n)s requested. We will ring you as soon as it is free.")
                             % {"n": req.target_number})
        except CallbackError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, _("Please check the callback form."))
    return redirect(_index_url(event))


@login_required
@with_event
@require_POST
def cancel(request, slug, pk, *, event):
    req = _visible_callback(request, event, pk)
    services.cancel(req, user=request.user, request=request)
    messages.success(request, _("Callback cancelled."))
    return redirect(request.POST.get("next") or _index_url(event))


@login_required
@with_event
@require_POST
def ringback(request, slug, *, event):
    form = TestRingbackForm(request.POST, extensions=_my_extensions(event, request.user))
    if form.is_valid():
        try:
            rb = services.request_test_ringback(event, form.cleaned_data["extension"].number,
                                                form.cleaned_data["delay"], user=request.user, request=request)
            messages.success(request, _("Your handset %(n)s will ring in %(s)s seconds.")
                             % {"n": rb.caller_number, "s": rb.delay_seconds})
        except CallbackError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, _("Please pick one of your extensions."))
    return redirect(_index_url(event))


@login_required
@with_event
@require_POST
def wakeup_new(request, slug, *, event):
    exts = _my_extensions(event, request.user)
    form = WakeupForm(request.POST, request.FILES, extensions=exts, event=event)
    if form.is_valid():
        d = form.cleaned_data
        try:
            call = services.schedule_wakeup(
                event, request.user, d["extension"], d["scheduled_for"], repeat=d["repeat"],
                announcement=d["announcement"], announcement_text=d.get("announcement_text") or "",
                announcement_file=d.get("announcement_file"), max_retries=d["max_retries"],
                retry_interval_minutes=d["retry_interval_minutes"], snooze_minutes=d["snooze_minutes"],
                request=request,
            )
            local = timezone.localtime(call.scheduled_for, services.event_tz(event))
            messages.success(request, _("Wake-up call scheduled for %(t)s.") % {"t": local.strftime("%a %H:%M")})
            return redirect(_index_url(event))
        except CallbackError as exc:
            form.add_error(None, str(exc))
    plan = get_plan(event)
    return render(request, "callback/wakeup_form.html", {"event": event, "form": form, "plan": plan,
                                                         "active": "index"})


@login_required
@with_event
@require_POST
def wakeup_cancel(request, slug, pk, *, event):
    call = _visible_scheduled(request, event, pk)
    services.cancel_scheduled(call, user=request.user, request=request)
    messages.success(request, _("Scheduled call cancelled."))
    return redirect(request.POST.get("next") or _index_url(event))


@login_required
@with_event
@require_POST
def wakeup_snooze(request, slug, pk, *, event):
    call = _visible_scheduled(request, event, pk)
    try:
        services.snooze(call, user=request.user, request=request)
        messages.success(request, _("Snoozed for %(m)s minutes.") % {"m": call.snooze_minutes})
    except CallbackError as exc:
        messages.error(request, str(exc))
    return redirect(request.POST.get("next") or _index_url(event))


# --------------------------------------------------------------------------- orga

@require_orga
def all_view(request, slug, *, event):
    state = request.GET.get("state") or ""
    cbs = CallbackRequest.objects.filter(event=event).select_related("requester__owner", "target__owner")
    calls = ScheduledCall.objects.filter(event=event).select_related("extension", "owner").order_by("-scheduled_for")
    if state == "open":
        cbs = cbs.filter(state__in=CallbackRequest.OPEN_STATES)
        calls = calls.filter(state__in=ScheduledCall.OPEN_STATES)
    return render(request, "callback/all.html", {
        "event": event, "callbacks": cbs[:200], "scheduled": calls[:200], "active": "all", "state": state,
        "ringbacks": TestRingback.objects.filter(event=event).select_related("extension")[:50],
        "now": timezone.now(),
    })


@require_orga
@require_POST
def fire_now(request, slug, kind, pk, *, event):
    back = reverse("callback:all", args=[event.slug])
    if kind == "callback":
        req = get_object_or_404(CallbackRequest, pk=pk, event=event)
        if not req.is_open:
            messages.error(request, _("This callback is no longer open."))
            return redirect(back)
        ok = services.deliver_callback(req)
    elif kind == "wakeup":
        call = get_object_or_404(ScheduledCall, pk=pk, event=event)
        if call.state == ScheduledCall.State.DIALING:
            messages.error(request, _("This call is already dialing."))
            return redirect(back)
        if not call.is_open:
            call.state = ScheduledCall.State.SCHEDULED
            call.save(update_fields=["state", "updated_at"])
        ok = services.fire_scheduled_call(call)
    else:
        rb = get_object_or_404(TestRingback, pk=pk, event=event)
        rb.state = TestRingback.State.SCHEDULED
        ok = services.fire_test_ringback(rb)
    audit(action="other", actor=request.user, event=event, request=request,
          message=f"Manual fire {kind} #{pk}: {'ok' if ok else 'failed'}")
    if ok:
        messages.success(request, _("Call originated."))
    else:
        messages.error(request, _("The PBX rejected the originate; see the state column."))
    return redirect(back)
