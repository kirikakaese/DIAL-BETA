"""Portal views for the devices app (mounted at ``/e/<slug>/devices/``).

Sub-paths deliberately avoid ``<uuid:pk>/…`` because ``portal:device_detail`` and friends already
live at ``/e/<slug>/devices/<uuid:pk>/``.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.extensions.models import ENDPOINT_TYPES, Extension
from apps.portal.shortcuts import require_orga, with_event

from . import services
from .forms import GSMDeviceForm, SuggestVendorForm, VendorForm
from .models import Device, DeviceType
from .services import DeviceServiceError


def _detail(event, device):
    return reverse("portal:device_detail", args=[event.slug, device.pk])


# ----------------------------------------------------------------------------- handset history

@login_required
@with_event
def history(request, slug, *, event):
    rows = []
    for h in services.handset_history(request.user):
        rows.append({"h": h, "here": h.device_in(event),
                     "reuse_from": next((u.device for u in h.uses if u.event.pk != event.pk), None)})
    return render(request, "devices/history.html", {"event": event, "rows": rows})


@login_required
@with_event
@require_POST
def reuse(request, slug, pk, *, event):
    old = get_object_or_404(Device.objects.select_related("event"), pk=pk)
    if old.owner_id != request.user.pk and not request.user.is_orga(event):
        raise PermissionDenied
    try:
        device, created = services.reuse_handset(old, event, request.user, request=request)
    except DeviceServiceError as exc:
        messages.error(request, str(exc))
        return redirect(reverse("devices:history", args=[event.slug]))
    if created:
        messages.success(request, _("Handset %(ipei)s added to this event. Bind it to an extension to get a PIN.")
                         % {"ipei": device.ipei})
    else:
        messages.info(request, _("This handset is already registered in this event."))
    return redirect(_detail(event, device))


@login_required
@with_event
@require_POST
def suggest_vendor(request, slug, pk, *, event):
    device = get_object_or_404(Device, pk=pk, event=event)
    if device.owner_id != request.user.pk and not request.user.is_orga(event):
        raise PermissionDenied
    form = SuggestVendorForm(request.POST)
    if not device.emc:
        messages.error(request, _("This device has no IPEI."))
    elif form.is_valid():
        try:
            m = services.suggest_vendor(device.emc, form.cleaned_data["name"], models_hint=device.handset_model,
                                        actor=request.user, event=event)
            messages.success(request, _("Thanks! EMC %(emc)s is now known as %(name)s.")
                             % {"emc": m.emc, "name": m.name})
        except DeviceServiceError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, _("Please enter a vendor name."))
    return redirect(_detail(event, device))


# ----------------------------------------------------------------------------- orga: manufacturers

@require_orga
def manufacturers(request, slug, *, event):
    form = VendorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            m = services.upsert_vendor(d["emc"], d["name"], models_hint=d["models_hint"], actor=request.user,
                                       event=event)
            messages.success(request, _("Saved %(emc)s → %(name)s.") % {"emc": m.emc, "name": m.name})
            return redirect(reverse("devices:manufacturers", args=[event.slug]))
        except DeviceServiceError as exc:
            form.add_error(None, str(exc))
    return render(request, "devices/manufacturers.html", {
        "event": event, "form": form, "rows": services.manufacturer_stats(event),
    })


# ----------------------------------------------------------------------------- orga: GSM

@require_orga
def gsm(request, slug, *, event):
    form = GSMDeviceForm(request.POST or None)
    if request.method == "POST" and event.has_gsm and form.is_valid():
        d = form.cleaned_data
        ext = None
        if d["number"]:
            ext = Extension.objects.filter(event=event, number=d["number"], type__in=ENDPOINT_TYPES).active().first()
            if ext is None:
                form.add_error("number", _("No active endpoint extension with that number."))
        if not form.errors:
            try:
                device = services.create_gsm_device(event, name=d["name"], msisdn=d["msisdn"], imsi=d["imsi"],
                                                    extension=ext, actor=request.user, request=request)
                messages.success(request, _("GSM device added. Registration code: %(t)s")
                                 % {"t": device.gsm_register_token})
                return redirect(reverse("devices:gsm", args=[event.slug]))
            except DeviceServiceError as exc:
                form.add_error(None, str(exc))
    return render(request, "devices/gsm.html", {
        "event": event, "form": form, "devices": services.gsm_devices(event) if event.has_gsm else [],
    })


@require_orga
@require_POST
def gsm_token(request, slug, pk, *, event):
    device = get_object_or_404(Device, pk=pk, event=event, type=DeviceType.GSM)
    token = device.issue_gsm_register_token()
    messages.success(request, _("New registration code for %(d)s: %(t)s") % {"d": device, "t": token})
    return redirect(reverse("devices:gsm", args=[event.slug]))
