"""Orga-only portal pages: live RFP dashboard, handsets, coverage map, alerts, site survey."""
import json

from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.audit import log as audit_log
from apps.dect import get_dect
from apps.devices.models import Device
from apps.extensions.services import get_plan
from apps.portal.shortcuts import require_orga

from . import claim, services
from .models import RFP, Alert, SiteSurveyLog, SyncCluster, VenueMap
from .reverse import dect_reverse


class VenueMapForm(forms.ModelForm):
    class Meta:
        model = VenueMap
        fields = ["name", "image", "is_default"]


class RFPForm(forms.ModelForm):
    class Meta:
        model = RFP
        fields = ["name", "location", "cluster", "venue_map", "is_active"]

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cluster"].queryset = SyncCluster.objects.filter(event=event)
        self.fields["venue_map"].queryset = VenueMap.objects.filter(event=event)


def _rfps(event):
    return (RFP.objects.filter(event=event).select_related("cluster", "venue_map")
            .annotate(n_handsets=Count("handsets")))


@require_orga
def index(request, slug, *, event):
    rfps = list(_rfps(event))
    clusters = list(SyncCluster.objects.filter(event=event).prefetch_related("rfps"))
    open_alerts = Alert.objects.filter(event=event, resolved_at__isnull=True).select_related("rfp")[:20]
    counts = {
        "rfps": len(rfps),
        "up": sum(1 for r in rfps if r.status == "up"),
        "down": sum(1 for r in rfps if r.status == "down"),
        "unsynced": sum(1 for r in rfps if r.status == "unsynced"),
        "handsets": Device.objects.filter(event=event, type="dect").count(),
        "subscribed": Device.objects.filter(event=event, type="dect", state="subscribed").count(),
        "calls": sum(r.active_calls for r in rfps),
    }
    return render(request, "dect/index.html", {
        "event": event, "rfps": rfps, "clusters": clusters, "open_alerts": open_alerts, "counts": counts,
        "weak_zones": services.weak_zones(event), "active": "index",
    })


@require_orga
def handsets(request, slug, *, event):
    q = request.GET.get("q", "").strip()
    devices = (Device.objects.filter(event=event, type="dect")
               .select_related("owner", "last_seen_rfp")
               .prefetch_related("bindings__extension"))
    if q:
        devices = devices.filter(
            Q(ipei__icontains=q) | Q(name__icontains=q) | Q(owner__username__icontains=q)
            | Q(bindings__extension__number__icontains=q) | Q(last_seen_rfp__name__icontains=q)
            | Q(omm_ppn=q)
        ).distinct()
    plan = get_plan(event)
    return render(request, "dect/handsets.html", {
        "event": event, "devices": devices.order_by("-last_seen_at", "ipei"), "q": q, "active": "handsets",
        "claim_number": plan.dect_claim_number, "pool_form": PoolHandsetForm(),
        "unclaimed": claim.unclaimed_devices(event).count(),
    })


class PoolHandsetForm(forms.Form):
    ipei = forms.CharField(label="IPEI", max_length=20, help_text=_("13 digits"))
    name = forms.CharField(label=_("Label"), max_length=80, required=False,
                           help_text=_("e.g. 'pool #12' or the sticker on the handset"))


@require_orga
@require_POST
def pool_add(request, slug, *, event):
    """Add a handset to the claim pool: DIAL creates the subscription with a PIN and a temporary number."""
    form = PoolHandsetForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Please enter a 13-digit IPEI."))
        return redirect(dect_reverse("handsets", kwargs=dict(slug=slug)))
    try:
        device = claim.add_pool_handset(event, form.cleaned_data["ipei"], actor=request.user,
                                        name=form.cleaned_data["name"])
    except ValueError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - DECT/PBX backend may be down; device is kept with the error
        messages.warning(request, _("Pool handset added, but the DECT system could not be updated: %(e)s")
                         % {"e": exc})
    else:
        messages.success(request, _("Pool handset %(ipei)s added - subscription PIN %(pin)s (30 min). It will show "
                                    "the temporary number %(n)s until claimed.")
                         % {"ipei": device.ipei, "pin": device.subscription_pin,
                            "n": device.config.get("temp_number", "")})
    return redirect(dect_reverse("handsets", kwargs=dict(slug=slug)))


@require_orga
@require_POST
def pool_remove(request, slug, *, event, pk):
    """Drop an unclaimed pool handset (deletes its OMM subscription)."""
    device = get_object_or_404(Device, pk=pk, event=event, type="dect", unclaimed=True)
    if device.omm_ppn:
        try:
            get_dect(event).delete_subscription(device.omm_ppn)
        except Exception as exc:  # noqa: BLE001
            messages.warning(request, _("Subscription could not be removed from the DECT system: %(e)s") % {"e": exc})
    from apps.pbx import get_pbx

    try:
        get_pbx(event).remove_device(device)
    except Exception as exc:  # noqa: BLE001
        messages.warning(request, _("PBX endpoint could not be removed: %(e)s") % {"e": exc})
    audit_log(action="delete", actor=request.user, target=device, event=event, request=request,
              message=f"Pool handset {device.ipei} removed")
    device.delete()
    messages.success(request, _("Pool handset removed."))
    return redirect(dect_reverse("handsets", kwargs=dict(slug=slug)))


@require_orga
def coverage_map(request, slug, *, event, map_id=None):
    maps = list(VenueMap.objects.filter(event=event))
    form = VenueMapForm()
    if request.method == "POST":
        form = VenueMapForm(request.POST, request.FILES)
        if form.is_valid():
            vm = form.save(commit=False)
            vm.event = event
            try:
                vm.width, vm.height = vm.image.width, vm.image.height
            except Exception:  # noqa: BLE001 - dimensions are optional
                pass
            vm.save()
            if vm.is_default:
                VenueMap.objects.filter(event=event).exclude(pk=vm.pk).update(is_default=False)
            audit_log(action="create", actor=request.user, target=vm, event=event, request=request,
                      message=_("Venue map uploaded"))
            messages.success(request, _("Map uploaded."))
            return redirect(dect_reverse("map_detail", kwargs=dict(slug=slug, map_id=vm.pk)))
    current = None
    if map_id:
        current = get_object_or_404(VenueMap, pk=map_id, event=event)
    elif maps:
        current = next((m for m in maps if m.is_default), maps[0])
    rfps = list(_rfps(event))
    placed = [r for r in rfps if current and r.venue_map_id == current.pk and r.pos_x is not None]
    unplaced = [r for r in rfps if not current or r.venue_map_id != current.pk or r.pos_x is None]
    return render(request, "dect/map.html", {
        "event": event, "maps": maps, "current": current, "form": form, "placed": placed, "unplaced": unplaced,
        "active": "map",
    })


@require_orga
@require_POST
def map_place(request, slug, *, event):
    """Place/move an RFP pin; accepts JSON or form data ``{rfp, map, x, y}`` (percent)."""
    data = request.POST or {}
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            return HttpResponseBadRequest("invalid JSON")
    try:
        rfp = RFP.objects.get(pk=int(data.get("rfp")), event=event)
        vm = VenueMap.objects.get(pk=int(data.get("map")), event=event)
        x, y = float(data.get("x")), float(data.get("y"))
    except (RFP.DoesNotExist, VenueMap.DoesNotExist, TypeError, ValueError):
        return HttpResponseBadRequest("rfp, map, x, y required")
    if data.get("remove"):
        rfp.venue_map, rfp.pos_x, rfp.pos_y = None, None, None
    else:
        rfp.venue_map, rfp.pos_x, rfp.pos_y = vm, max(0.0, min(100.0, x)), max(0.0, min(100.0, y))
    rfp.save(update_fields=["venue_map", "pos_x", "pos_y", "updated_at"])
    return JsonResponse({"ok": True, "rfp": rfp.pk, "x": rfp.pos_x, "y": rfp.pos_y, "status": rfp.status})


@require_orga
def alerts(request, slug, *, event):
    show_all = request.GET.get("all") == "1"
    qs = Alert.objects.filter(event=event).select_related("rfp")
    if not show_all:
        qs = qs.filter(resolved_at__isnull=True)
    return render(request, "dect/alerts.html", {"event": event, "alerts": qs[:200], "show_all": show_all,
                                                "active": "alerts"})


@require_orga
@require_POST
def alert_resolve(request, slug, *, event, pk):
    alert = get_object_or_404(Alert, pk=pk, event=event)
    services.resolve_alert(alert)
    audit_log(action="resolve", actor=request.user, target=alert, event=event, request=request,
              message=_("Alert resolved manually"))
    messages.success(request, _("Alert resolved."))
    return redirect(dect_reverse("alerts", kwargs=dict(slug=slug)))


@require_orga
def survey(request, slug, *, event):
    logs = SiteSurveyLog.objects.filter(event=event).select_related("device", "device__owner", "rfp")[:200]
    return render(request, "dect/survey.html", {
        "event": event, "logs": logs, "survey_number": get_plan(event).site_survey_number, "active": "survey",
    })


@require_orga
def rfp_edit(request, slug, *, event, pk):
    rfp = get_object_or_404(RFP, pk=pk, event=event)
    form = RFPForm(request.POST or None, instance=rfp, event=event)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit_log(action="update", actor=request.user, target=rfp, event=event, request=request,
                  message=_("RFP edited"), changes=form.changed_data and {k: str(form.cleaned_data[k])
                                                                          for k in form.changed_data})
        messages.success(request, _("RFP saved."))
        return redirect(dect_reverse("index", kwargs=dict(slug=slug)))
    samples = rfp.samples.all()[:48]
    return render(request, "dect/rfp_edit.html", {"event": event, "rfp": rfp, "form": form, "samples": samples,
                                                  "handsets": rfp.handsets.select_related("owner"),
                                                  "active": "index"})


@require_orga
@require_POST
def sync_now(request, slug, *, event):
    try:
        result = services.sync_infrastructure(event)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, _("Sync failed: %(err)s") % {"err": exc})
    else:
        if result.get("ok"):
            messages.success(request, _("Synced %(r)d RFPs and %(h)d handsets.")
                             % {"r": result["rfps"], "h": result["handsets"]})
        else:
            messages.error(request, _("DECT system unreachable: %(err)s") % {"err": result.get("error")})
    nxt = request.POST.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(dect_reverse("index", kwargs=dict(slug=slug)))
