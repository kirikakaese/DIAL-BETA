"""Portal views: orga/helpdesk statistics dashboard, CSV export and the per-user "my calls" / GDPR page."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.audit import log as audit
from apps.core.features import require
from apps.portal.shortcuts import require_helpdesk, require_orga, with_event

from . import services

HOURS_CHOICES = (6, 12, 24, 48, 72, 168)


def parse_hours(value, default: int = 48) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(hours, 24 * 31))


@require_helpdesk
@require("stats")
def index(request, slug, *, event):
    hours = parse_hours(request.GET.get("hours"))
    data = services.dashboard_data(event, hours=hours)
    chart = {
        "labels": [p["label"] for p in data["series"]],
        "series": [
            {"name": _("Calls"), "values": [p["calls"] for p in data["series"]]},
            {"name": _("Answered"), "values": [p["answered"] for p in data["series"]], "color": "--ok"},
        ],
    }
    ctx = {
        "event": event, "data": data, "hours": hours, "hours_choices": HOURS_CHOICES,
        "chart": chart, "active": "index", "now": timezone.now(),
        "active_extensions": event.extensions.active().count(),
    }
    return render(request, "stats/index.html", ctx)


@require_orga
@require("stats")
def export_csv(request, slug, *, event):
    body = services.export_csv(event)
    audit(action="other", actor=request.user, event=event, request=request, message="CDR CSV export")
    resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
    kind = "hourly" if event.cdr_aggregate_only else "cdr"
    resp["Content-Disposition"] = f'attachment; filename="{event.slug}-{kind}-{timezone.now():%Y%m%d-%H%M}.csv"'
    return resp


@login_required
@with_event
@require("stats")
def mine(request, slug, *, event):
    calls = services.my_calls(request.user, event=event)
    return render(request, "stats/mine.html", {
        "event": event, "calls": calls, "active": "mine", "aggregate_only": event.cdr_aggregate_only,
        "retention_days": services.retention_days(event),
    })


@login_required
@with_event
def gdpr_export(request, slug, *, event):
    data = services.gdpr_export(request.user)
    resp = JsonResponse(data, json_dumps_params={"indent": 2, "ensure_ascii": False})
    resp["Content-Disposition"] = f'attachment; filename="dial-calls-{request.user.username}.json"'
    return resp


@login_required
@with_event
@require_POST
def gdpr_delete(request, slug, *, event):
    if request.POST.get("confirm") != "yes":
        messages.error(request, _("Please tick the confirmation box to anonymize your call records."))
    else:
        n = services.gdpr_delete(request.user)
        messages.success(request, _("%(n)s call records were anonymized.") % {"n": n})
    return redirect(reverse("stats:mine", args=[event.slug]))
