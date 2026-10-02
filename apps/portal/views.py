"""Portal views - core self-service and orga pages.

All extension state changes go through ``apps.extensions.services``; this
module only deals with forms, permissions and rendering.
"""
from __future__ import annotations

import base64
import json
from collections import OrderedDict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import UploadedFile
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.models import ServiceAccount, User
from apps.core import features
from apps.core.audit import log as audit
from apps.core.models import AuditLog
from apps.devices.models import Device, DeviceBinding, DeviceType
from apps.devices.qr import qr_png
from apps.events.export import export_event, import_event
from apps.events.models import Event, EventMembership, UserGroup, Webhook
from apps.extensions import services
from apps.extensions.history import looks_like_number, number_history
from apps.extensions.models import Extension, ExtensionRequest, ExtensionTransfer, ExtensionType
from apps.extensions.services import ExtensionError, get_plan
from apps.numbering.models import NumberRange

from . import forms
from .shortcuts import require_helpdesk, require_orga, with_event

LIVE_STATES = [Extension.State.REQUESTED, Extension.State.ACTIVE, Extension.State.SUSPENDED]

# Cards on the event dashboard: (feature flag or None, url name, label, description)
FEATURE_CARDS = [
    (None, "callback:index", _("Callbacks & wake-up"), _("Ring me back when busy, test ringback, wake-up calls.")),
    ("phonebook", "phonebook:index", _("Phonebook"), _("Who has which number - searchable and exportable.")),
    ("callgroups", "callgroups:index", _("Call groups"), _("Hunt groups: one number, many handsets.")),
    ("voicemail", "voicemail:index", _("Voicemail"), _("Listen to messages left on your extensions.")),
    ("messaging", "messaging:index", _("Messaging"), _("Send text messages to DECT handsets.")),
    ("ivr", "ivr:index", _("Announcements & IVR"), _("Build menus and announcements for your number.")),
    ("conferences", "conferences:index", _("Conferences"), _("Ad-hoc conference rooms.")),
    ("stats", "stats:mine", _("My calls"), _("Your own call history and statistics.")),
]


# ----------------------------------------------------------------------------- helpers

def _ext_or_403(request, event, pk, *, edit=False):
    """Extension in ``event``; owner may see and edit, helpdesk may see, orga may edit."""
    ext = get_object_or_404(Extension.objects.select_related("owner", "event"), pk=pk, event=event)
    user = request.user
    is_owner = ext.owner_id == user.pk
    if edit:
        if not (is_owner or user.is_orga(event)):
            raise PermissionDenied
    elif not (is_owner or user.is_helpdesk(event)):
        raise PermissionDenied
    return ext


def _device_or_403(request, event, pk):
    dev = get_object_or_404(Device.objects.select_related("owner", "event", "last_seen_rfp"), pk=pk, event=event)
    user = request.user
    if dev.owner_id != user.pk and not user.is_helpdesk(event):
        # an owner of a bound extension may also look at the device
        if not dev.bindings.filter(extension__owner=user).exists():
            raise PermissionDenied
    return dev


def _my_extensions(event, user):
    return (Extension.objects.filter(event=event, owner=user).exclude(state=Extension.State.DELETED)
            .annotate(device_count=Count("bindings", distinct=True)).order_by("number"))


def _feature_cards(event):
    cards = []
    for flag, urlname, label, desc in FEATURE_CARDS:
        if flag and not features.enabled(flag, event):
            continue
        try:
            url = reverse(urlname, args=[event.slug])
        except Exception:  # noqa: BLE001 - app not mounted
            continue
        cards.append({"url": url, "label": label, "description": desc})
    return cards


def _audit_for(obj, limit=30):
    ct = ContentType.objects.get_for_model(obj)
    return AuditLog.objects.filter(target_type=ct, target_id=str(obj.pk)).select_related("actor")[:limit]


def _json_download(data, filename):
    body = json.dumps(data, cls=DjangoJSONEncoder, indent=2, ensure_ascii=False)
    resp = HttpResponse(body, content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def _back(request, default_url):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(default_url)


# ----------------------------------------------------------------------------- public / user

def home(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    events = Event.objects.visible_to(request.user).live().order_by("start_date")
    return render(request, "portal/home.html", {"events": events})


@login_required
def dashboard(request):
    user = request.user
    exts = (Extension.objects.filter(owner=user).exclude(state=Extension.State.DELETED)
            .select_related("event").annotate(device_count=Count("bindings", distinct=True))
            .order_by("-event__start_date", "number"))
    by_event: OrderedDict = OrderedDict()
    for e in exts:
        by_event.setdefault(e.event, []).append(e)
    member_event_ids = set(user.memberships.values_list("event_id", flat=True))
    joinable = (Event.objects.visible_to(user).live().filter(is_public=True)
                .exclude(pk__in=member_event_ids).order_by("start_date"))
    my_events = Event.objects.filter(pk__in=member_event_ids).live().order_by("start_date")
    portable = []
    for ev in my_events:
        if ev not in by_event and services.portable_extensions(user, ev):
            portable.append(ev)
    transfers = (ExtensionTransfer.objects.filter(to_user=user, accepted_at__isnull=True,
                                                  expires_at__gt=timezone.now())
                 .select_related("extension__event", "from_user"))
    return render(request, "portal/dashboard.html", {
        "by_event": by_event, "joinable": joinable, "portable": portable, "transfers": transfers,
        "my_events": my_events,
    })


def event_list(request):
    events = Event.objects.visible_to(request.user).order_by("-start_date")
    roles = {}
    if request.user.is_authenticated:
        roles = dict(request.user.memberships.values_list("event_id", "role"))
    rows = [{"event": e, "role": "admin" if request.user.is_superuser else roles.get(e.pk)} for e in events]
    return render(request, "portal/event_list.html", {"rows": rows})


@login_required
def event_create(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    form = forms.EventCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ev = form.save()
        get_plan(ev)
        EventMembership.objects.get_or_create(event=ev, user=request.user,
                                              defaults={"role": EventMembership.Role.ADMIN})
        audit(action="create", actor=request.user, target=ev, event=ev, request=request, message="Event created")
        messages.success(request, _("Event created. Configure the number plan next."))
        return redirect("portal:orga_numberplan", ev.slug)
    return render(request, "portal/event_form.html", {"form": form})


@with_event
def switch_event(request, slug, *, event):
    return redirect("portal:event_dashboard", event.slug)


@login_required
@with_event
@require_POST
def event_join(request, slug, *, event):
    if not event.is_public or event.is_archived:
        messages.error(request, _("This event cannot be joined."))
        return redirect("portal:event_dashboard", event.slug)
    form = forms.EventJoinForm(request.POST)
    form.is_valid()
    m, created = EventMembership.objects.get_or_create(event=event, user=request.user)
    if created:
        audit(action="create", actor=request.user, target=m, event=event, request=request, message="Joined event")
        messages.success(request, _("Welcome to %(e)s!") % {"e": event.name})
    code = (form.cleaned_data.get("join_code") or "").strip()
    if code:
        grp = UserGroup.objects.filter(event=event, join_code=code).exclude(join_code="").first()
        if grp:
            m.groups.add(grp)
            messages.success(request, _("You joined the group '%(g)s'.") % {"g": grp.name})
        else:
            messages.error(request, _("Unknown group join code."))
    return redirect("portal:event_dashboard", event.slug)


@with_event
def event_dashboard(request, slug, *, event):
    user = request.user
    ctx = {"event": event, "plan": get_plan(event), "cards": _feature_cards(event), "my_extensions": [],
           "is_member": False, "portable_count": 0, "join_form": forms.EventJoinForm()}
    if user.is_authenticated:
        ctx["my_extensions"] = _my_extensions(event, user).prefetch_related("bindings__device")
        ctx["is_member"] = user.is_superuser or event.memberships.filter(user=user).exists()
        ctx["portable_count"] = len(services.portable_extensions(user, event))
        ctx["pending_transfers"] = ExtensionTransfer.objects.filter(
            to_user=user, extension__event=event, accepted_at__isnull=True, expires_at__gt=timezone.now()).count()
    return render(request, "portal/event_dashboard.html", ctx)


@login_required
@with_event
def extension_list(request, slug, *, event):
    exts = _my_extensions(event, request.user).prefetch_related("bindings__device")
    return render(request, "portal/extension_list.html", {"event": event, "extensions": exts})


@login_required
@with_event
def extension_create(request, slug, *, event):
    initial = {"number": request.GET.get("number", "")}
    if request.GET.get("type"):
        initial["type"] = request.GET["type"]
    form = forms.ExtensionCreateForm(request.POST or None, event=event, user=request.user, initial=initial)
    failure = None
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            ext = services.register(
                event, request.user, d["number"], d["type"], request=request,
                display_name=d["display_name"], description=d["description"], location_hint=d["location_hint"],
                in_phonebook=d["in_phonebook"], request_note=d["request_note"], config=form.config,
            )
        except ExtensionError as exc:
            form.add_error(None, str(exc))
            av = services.check_availability(event, d["number"], user=request.user, extension_type=d["type"],
                                             block_digits=form.config.get("block_digits", 0))
            failure = {"taken": av.taken, "suggestions": av.suggestions,
                       "can_waitlist": av.taken and features.enabled("waitlist", event)}
        else:
            if ext.is_active:
                messages.success(request, _("Extension %(n)s is active. Add a device to start calling.")
                                 % {"n": ext.number})
            else:
                messages.info(request, _("Extension %(n)s requested - awaiting approval by the orga team.")
                              % {"n": ext.number})
            return redirect("portal:extension_detail", event.slug, ext.pk)
    return render(request, "portal/extension_form.html", {
        "event": event, "form": form, "failure": failure, "plan": get_plan(event),
        "quota_used": Extension.objects.filter(event=event, owner=request.user, state__in=LIVE_STATES).count(),
    })


@login_required
@with_event
@require_POST
def waitlist_join(request, slug, *, event):
    if not features.enabled("waitlist", event):
        raise Http404
    number = (request.POST.get("number") or "").strip()
    ext_type = request.POST.get("type") or ExtensionType.DECT
    if ext_type not in ExtensionType.values:
        ext_type = ExtensionType.DECT
    try:
        services.join_waitlist(event, request.user, number, extension_type=ext_type)
        audit(action="create", actor=request.user, event=event, request=request,
              message=f"Joined waitlist for {number}")
        messages.success(request, _("You are on the waitlist for %(n)s. We will notify you when it becomes free.")
                         % {"n": number})
    except ExtensionError as exc:
        messages.error(request, str(exc))
    return redirect(f"{reverse('portal:extension_create', args=[event.slug])}?number={number}")


@login_required
@with_event
def extension_port(request, slug, *, event):
    portable = services.portable_extensions(request.user, event)
    results = []
    if request.method == "POST":
        wanted = set(request.POST.getlist("ext"))
        for src in portable:
            if str(src.pk) not in wanted:
                continue
            try:
                ext = services.port(src, event, request.user, request=request)
                results.append({"number": src.number, "ok": True, "ext": ext,
                                "state": ext.get_state_display()})
            except ExtensionError as exc:
                results.append({"number": src.number, "ok": False, "error": str(exc)})
        portable = services.portable_extensions(request.user, event)
    return render(request, "portal/extension_port.html", {"event": event, "portable": portable,
                                                          "results": results})


@login_required
@with_event
def extension_detail(request, slug, *, event, pk):
    ext = _ext_or_403(request, event, pk)
    bindings = ext.bindings.select_related("device__last_seen_rfp").order_by("priority")
    is_owner = ext.owner_id == request.user.pk
    can_edit = is_owner or request.user.is_orga(event)
    plan = get_plan(event)
    from apps.dect.claim import claim_dial_string

    return render(request, "portal/extension_detail.html", {
        "event": event, "ext": ext, "bindings": bindings, "plan": plan,
        "claim_dial": claim_dial_string(plan, ext),
        "is_owner": is_owner, "can_edit": can_edit, "is_orga": request.user.is_orga(event),
        "endpoint_types": forms.compatible_endpoint_types(ext) if ext.accepts_devices else [],
        "dect_devices": [b.device for b in bindings if b.device.type == DeviceType.DECT],
        "sip_devices": [b.device for b in bindings if b.device.type != DeviceType.DECT],
        "trunk_device": next((b.device for b in bindings if b.device.type == DeviceType.SIP), None)
        if ext.is_trunk else None,
        "audit": _audit_for(ext), "now": timezone.now(),
        "transfers": ext.transfers.filter(accepted_at__isnull=True, expires_at__gt=timezone.now())
        .select_related("to_user"),
        "forwarded_from": ext.live_forwarders().select_related("owner").order_by("number"),
    })


@login_required
@with_event
@require_POST
def extension_new_claim_code(request, slug, *, event, pk):
    """Mint a new DECT claim code (invalidates the old one, e.g. after showing it around)."""
    ext = _ext_or_403(request, event, pk, edit=True)
    ext.issue_dect_claim_code()
    audit(action="update", actor=request.user, target=ext, event=event, request=request,
          message="New DECT claim code issued")
    messages.success(request, _("New claim code issued. The old code no longer works."))
    return redirect("portal:extension_detail", event.slug, ext.pk)


@login_required
@with_event
def extension_edit(request, slug, *, event, pk):
    ext = _ext_or_403(request, event, pk, edit=True)
    # ModelForm validation writes into its instance; keep ``ext`` pristine so services.update sees the diff.
    form = forms.ExtensionEditForm(request.POST or None, request.FILES or None,
                                   instance=Extension.objects.get(pk=ext.pk), user=request.user)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        data.pop("forward_target_number", None)
        tone = data.pop("ringback_tone", None)
        fwd_mode = data.pop("forward_mode", None)
        fwd_target = data.pop("forward_target", None)
        fwd_delay = data.pop("forward_delay", None)
        try:
            services.update(ext, request.user, request=request, **data)
            if fwd_mode is not None:
                services.set_forwarding(ext, request.user, request=request, mode=fwd_mode, target=fwd_target,
                                        delay=fwd_delay)
            if tone is False:
                services.clear_ringback_tone(ext, request.user, request=request)
            elif isinstance(tone, UploadedFile):
                services.set_ringback_tone(ext, tone, request.user, request=request)
                messages.info(request, _("Your ringback tone is being converted - this takes a moment."))
        except ExtensionError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Settings saved."))
            return redirect("portal:extension_detail", event.slug, ext.pk)
    return render(request, "portal/extension_edit.html", {"event": event, "ext": ext, "form": form})


@login_required
@with_event
def extension_delete(request, slug, *, event, pk):
    ext = _ext_or_403(request, event, pk, edit=True)
    if request.method == "POST":
        services.delete(ext, request.user, request=request)
        messages.success(request, _("Extension %(n)s deleted.") % {"n": ext.number})
        return redirect("portal:extension_list", event.slug)
    return render(request, "portal/extension_confirm_delete.html", {"event": event, "ext": ext})


@login_required
@with_event
def extension_transfer(request, slug, *, event, pk):
    ext = _ext_or_403(request, event, pk, edit=True)
    form = forms.TransferForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            tr = services.start_transfer(ext, request.user, form.cleaned_data["recipient"], request=request)
            messages.success(request, _("Transfer offered to %(u)s. Share the link below with them.")
                             % {"u": tr.to_user})
            return redirect("portal:extension_transfer", event.slug, ext.pk)
        except ExtensionError as exc:
            form.add_error(None, str(exc))
    open_transfers = ext.transfers.filter(accepted_at__isnull=True, expires_at__gt=timezone.now()).select_related(
        "to_user")
    return render(request, "portal/extension_transfer.html", {
        "event": event, "ext": ext, "form": form, "transfers": open_transfers,
        "public_url": settings.PET_PUBLIC_URL,
    })


@login_required
def accept_transfer(request, token):
    tr = get_object_or_404(ExtensionTransfer.objects.select_related("extension__event", "from_user", "to_user"),
                           token=token)
    if request.method == "POST":
        try:
            ext = services.accept_transfer(tr, request.user, request=request)
            messages.success(request, _("Extension %(n)s is now yours.") % {"n": ext.number})
            return redirect("portal:extension_detail", ext.event.slug, ext.pk)
        except ExtensionError as exc:
            messages.error(request, str(exc))
            return redirect("portal:dashboard")
    return render(request, "portal/accept_transfer.html", {"tr": tr, "ext": tr.extension,
                                                           "event": tr.extension.event,
                                                           "mine": tr.to_user_id == request.user.pk})


# ----------------------------------------------------------------------------- devices

@login_required
@with_event
def device_add(request, slug, *, event, pk):
    ext = _ext_or_403(request, event, pk, edit=True)
    if not ext.accepts_devices:
        messages.error(request, _("This extension type does not ring devices."))
        return redirect("portal:extension_detail", event.slug, ext.pk)
    if ext.is_trunk and ext.bindings.exists():
        messages.error(request, _("A SIP trunk has exactly one SIP account. Detach the existing one first."))
        return redirect("portal:extension_detail", event.slug, ext.pk)
    form = forms.DeviceAddForm(request.POST or None, extension=ext, user=request.user)
    if not form.types:
        messages.error(request, _("No device class is enabled for this extension type."))
        return redirect("portal:extension_detail", event.slug, ext.pk)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        device = d.get("existing_device")
        if device is None:
            device = Device(event=event, owner=ext.owner or request.user, type=d["endpoint_type"])
            for f in form.user_fields:
                if f in d and d[f] is not None:
                    setattr(device, f, d[f])
            device.save()
            device.ensure_sip_credentials()
            if device.type == DeviceType.DECT:
                device.issue_subscription_pin()
            audit(action="create", actor=request.user, target=device, event=event, request=request,
                  message=f"Device added to {ext.number}")
        else:
            device.ensure_sip_credentials()
        DeviceBinding.objects.get_or_create(extension=ext, device=device,
                                            defaults={"priority": ext.bindings.count()})
        from apps.extensions.tasks import provision_extension

        if ext.is_active:
            provision_extension.delay(str(ext.pk))
        if device.type == DeviceType.DECT:
            messages.success(request, _("Handset added. Subscribe it now with PIN %(pin)s (valid 30 minutes).")
                             % {"pin": device.subscription_pin})
        else:
            messages.success(request, _("Device added. Scan the QR code or copy the SIP credentials."))
        return redirect("portal:device_detail", event.slug, device.pk)
    return render(request, "portal/device_add.html", {"event": event, "ext": ext, "form": form,
                                                      "types": form.types})


@login_required
@with_event
def device_detail(request, slug, *, event, pk):
    dev = _device_or_403(request, event, pk)
    can_edit = dev.owner_id == request.user.pk or request.user.is_orga(event)
    form = forms.DeviceRenameForm(request.POST or None, instance=dev)
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        if form.is_valid():
            form.save()
            messages.success(request, _("Device saved."))
            return redirect("portal:device_detail", event.slug, dev.pk)
    return render(request, "portal/device_detail.html", {
        "event": event, "device": dev, "form": form, "can_edit": can_edit,
        "bindings": dev.bindings.select_related("extension"), "now": timezone.now(),
        "provision_error": (dev.config or {}).get("provision_error"),
        "audit": _audit_for(dev, 15),
    })


@login_required
@with_event
@require_POST
def device_new_pin(request, slug, *, event, pk):
    dev = _device_or_403(request, event, pk)
    if dev.type != DeviceType.DECT:
        messages.error(request, _("Only DECT handsets use subscription PINs."))
        return _back(request, reverse("portal:device_detail", args=[event.slug, dev.pk]))
    pin = dev.issue_subscription_pin()
    if dev.owner_id != request.user.pk:
        audit(action="impersonate", actor=request.user, target=dev, event=event, request=request,
              message=f"Helpdesk issued a new DECT PIN for {dev.owner}")
    else:
        audit(action="update", actor=request.user, target=dev, event=event, request=request,
              message="New subscription PIN issued")
    try:
        from apps.dect.provisioning import provision_device

        provision_device(dev, actor=request.user)
    except Exception as exc:  # noqa: BLE001 - DECT backend may be unreachable
        messages.warning(request, _("PIN issued, but the DECT system could not be updated: %(e)s") % {"e": exc})
    messages.success(request, _("New PIN: %(pin)s - valid for 30 minutes.") % {"pin": pin})
    return _back(request, reverse("portal:device_detail", args=[event.slug, dev.pk]))


@login_required
@with_event
@require_POST
def device_rotate_sip(request, slug, *, event, pk):
    dev = _device_or_403(request, event, pk)
    if dev.owner_id != request.user.pk and not request.user.is_orga(event):
        raise PermissionDenied
    dev.ensure_sip_credentials(save=False)
    dev.rotate_sip_password()
    audit(action="update", actor=request.user, target=dev, event=event, request=request,
          message="SIP password rotated")
    try:
        from apps.pbx import outbox

        outbox.enqueue("sync_device", event=event, target=dev)
    except Exception as exc:  # noqa: BLE001
        messages.warning(request, _("Password rotated, but the PBX could not be updated: %(e)s") % {"e": exc})
    messages.success(request, _("SIP password rotated. Update your softphone."))
    return redirect("portal:device_detail", event.slug, dev.pk)


@login_required
@with_event
def device_qr(request, slug, *, event, pk):
    dev = _device_or_403(request, event, pk)
    if not dev.sip_username or not dev.sip_password or not dev.provisioning_token:
        dev.ensure_sip_credentials()
    links = dev.softphone_links()
    client = request.GET.get("client", "generic")
    if client not in links:
        raise Http404
    resp = HttpResponse(qr_png(links[client]), content_type="image/png")
    resp["Cache-Control"] = "private, no-store"
    return resp


@login_required
@with_event
@require_POST
def device_unbind(request, slug, *, event, pk, ext_pk):
    dev = _device_or_403(request, event, pk)
    ext = _ext_or_403(request, event, ext_pk, edit=True)
    binding = get_object_or_404(DeviceBinding, device=dev, extension=ext)
    binding.delete()
    audit(action="delete", actor=request.user, target=ext, event=event, request=request,
          message=f"Device {dev} unbound")
    if not dev.bindings.exists():
        dev.delete()
        messages.success(request, _("Device removed."))
    else:
        messages.success(request, _("Device detached from %(n)s.") % {"n": ext.number})
    from apps.extensions.tasks import provision_extension

    if ext.is_active:
        provision_extension.delay(str(ext.pk))
    return redirect("portal:extension_detail", event.slug, ext.pk)


@login_required
def claim_guest(request, token):
    ext = get_object_or_404(Extension.objects.select_related("event"), claim_token=token, is_temporary=True)
    if request.method == "POST":
        try:
            ext = services.claim_guest_extension(token, request.user, request=request)
            EventMembership.objects.get_or_create(event=ext.event, user=request.user)
            messages.success(request, _("Extension %(n)s is now yours. Add your handset next.") % {"n": ext.number})
            return redirect("portal:extension_detail", ext.event.slug, ext.pk)
        except ExtensionError as exc:
            messages.error(request, str(exc))
            return redirect("portal:dashboard")
    return render(request, "portal/claim_guest.html", {"ext": ext, "event": ext.event})


# ----------------------------------------------------------------------------- orga

def _counts(qs, field):
    return {row[field]: row["n"] for row in qs.values(field).annotate(n=Count("id"))}


@require_orga
def orga_dashboard(request, slug, *, event):
    exts = Extension.objects.filter(event=event)
    devs = Device.objects.filter(event=event)
    ctx = {
        "event": event, "active": "dashboard",
        "ext_by_state": _counts(exts, "state"),
        "ext_by_type": _counts(exts.filter(state__in=LIVE_STATES), "type"),
        "pending": exts.filter(state=Extension.State.REQUESTED).count(),
        "dev_by_type": _counts(devs, "type"), "dev_by_state": _counts(devs, "state"),
        "members": event.memberships.count(),
        "recent_audit": AuditLog.objects.filter(event=event).select_related("actor")[:15],
        "transitions": _allowed_transitions(event),
        "waitlist": ExtensionRequest.objects.filter(event=event).count(),
    }
    try:
        from apps.dect.models import RFP, Alert

        rfps = RFP.objects.filter(event=event, is_active=True)
        ctx["rfp_up"] = rfps.filter(connected=True).count()
        ctx["rfp_down"] = rfps.filter(connected=False).count()
        ctx["alerts"] = Alert.objects.filter(event=event, resolved_at__isnull=True)[:10]
        ctx["alert_count"] = Alert.objects.filter(event=event, resolved_at__isnull=True).count()
    except ImportError:  # pragma: no cover
        pass
    try:
        from apps.pbx import outbox

        ctx["pbx_queue"] = outbox.queue_stats(event)
    except Exception:  # noqa: BLE001 - the dashboard must render even if the outbox table is missing
        ctx["pbx_queue"] = None
    from apps.dect.models import DECTConnection
    from apps.pbx.models import PBXConnection

    ctx["venue"] = {
        "pbx": PBXConnection.objects.filter(event=event).first(),
        "dect": DECTConnection.objects.filter(event=event).first(),
    }
    ctx["venue_agent"] = None
    if ctx["venue"]["pbx"] is not None and ctx["venue"]["pbx"].is_agent:
        try:
            conn = ctx["venue"]["pbx"]
            status = "stale" if conn.agent_is_stale else ("behind" if conn.agent_behind else "ok")
            ctx["venue_agent"] = {"status": status, "last_seen": conn.agent_last_seen, "host": conn.agent_host}
        except Exception:  # noqa: BLE001 - the dashboard must render even if the snapshot cannot be built
            ctx["venue_agent"] = {"status": "unknown", "last_seen": None, "host": ""}
    return render(request, "portal/orga/dashboard.html", ctx)


def _allowed_transitions(event):
    S = Event.State
    table = {S.DRAFT: [S.REGISTRATION], S.REGISTRATION: [S.LIVE, S.DRAFT], S.LIVE: [S.ARCHIVED, S.REGISTRATION],
             S.ARCHIVED: [S.LIVE]}
    return [(s.value, s.label) for s in table[S(event.state)]]


@require_orga
def orga_event_settings(request, slug, *, event):
    form = forms.EventSettingsForm(request.POST or None, request.FILES or None, instance=event)
    if request.method == "POST" and form.is_valid():
        changes = {f: [form.initial.get(f), form.cleaned_data.get(f)] for f in form.changed_data
                   if f not in ("logo",)}
        form.save()
        audit(action="update", actor=request.user, target=event, event=event, request=request,
              message="Event settings", changes={k: [str(a), str(b)] for k, (a, b) in changes.items()})
        messages.success(request, _("Event settings saved."))
        return redirect("portal:orga_event_settings", event.slug)
    return render(request, "portal/orga/settings.html", {"event": event, "form": form, "active": "settings",
                                                         "transitions": _allowed_transitions(event)})


@require_orga
@require_POST
def orga_event_state(request, slug, *, event, state):
    try:
        event.transition(state, request.user)
        messages.success(request, _("Event is now '%(s)s'.") % {"s": event.get_state_display()})
    except ValueError as exc:
        messages.error(request, str(exc))
    return _back(request, reverse("portal:orga_dashboard", args=[event.slug]))


@require_orga
def orga_event_clone(request, slug, *, event):
    if not request.user.is_superuser:
        raise PermissionDenied
    form = forms.EventCloneForm(request.POST or None, initial={"name": f"{event.name} (copy)"})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        new = event.clone(name=d["name"], slug=d["slug"], start_date=d["start_date"], end_date=d["end_date"],
                          actor=request.user)
        EventMembership.objects.get_or_create(event=new, user=request.user,
                                              defaults={"role": EventMembership.Role.ADMIN})
        messages.success(request, _("Event cloned. It starts in state 'draft'."))
        return redirect("portal:orga_dashboard", new.slug)
    return render(request, "portal/orga/clone.html", {"event": event, "form": form, "active": "settings"})


@require_orga
def orga_numberplan(request, slug, *, event):
    plan = get_plan(event)
    form = forms.NumberPlanForm(request.POST or None, instance=plan)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(action="update", actor=request.user, target=plan, event=event, request=request,
              message="Number plan updated", changes={f: [str(form.initial.get(f)), str(form.cleaned_data.get(f))]
                                                      for f in form.changed_data})
        messages.success(request, _("Number plan saved."))
        return redirect("portal:orga_numberplan", event.slug)
    test_form = forms.NumberTestForm(request.GET or None)
    test = None
    if request.GET.get("number") and test_form.is_valid():
        n = test_form.cleaned_data["number"].strip()
        as_user = forms.find_user(test_form.cleaned_data.get("as_user")) or None
        av = services.check_availability(event, n, user=as_user)
        test = {"number": n, "policy": av.policy, "taken": av.taken, "available": av.available,
                "reason": av.reason, "as_user": as_user, "quota_ok": av.quota_ok}
    ranges = plan.ranges.prefetch_related("allowed_groups").order_by("priority", "id")
    return render(request, "portal/orga/numberplan.html", {
        "event": event, "plan": plan, "form": form, "ranges": ranges, "test_form": test_form, "test": test,
        "active": "numberplan",
    })


@require_orga
def orga_range_edit(request, slug, *, event, pk=None):
    plan = get_plan(event)
    rng = get_object_or_404(NumberRange, pk=pk, plan=plan) if pk else NumberRange(plan=plan)
    form = forms.NumberRangeForm(request.POST or None, instance=rng, event=event)
    if request.method == "POST" and form.is_valid():
        rng = form.save()
        audit(action="update" if pk else "create", actor=request.user, target=rng, event=event, request=request,
              message=f"Number range '{rng.name}' saved")
        messages.success(request, _("Range '%(n)s' saved.") % {"n": rng.name})
        return redirect("portal:orga_numberplan", event.slug)
    return render(request, "portal/orga/range_form.html", {"event": event, "form": form, "range": rng,
                                                           "active": "numberplan"})


@require_orga
@require_POST
def orga_range_delete(request, slug, *, event, pk):
    rng = get_object_or_404(NumberRange, pk=pk, plan__event=event)
    audit(action="delete", actor=request.user, target=rng, event=event, request=request,
          message=f"Number range '{rng.name}' deleted")
    rng.delete()
    messages.success(request, _("Range deleted."))
    return redirect("portal:orga_numberplan", event.slug)


@require_orga
def orga_queue(request, slug, *, event):
    plan = get_plan(event)
    exts = (Extension.objects.filter(event=event, state=Extension.State.REQUESTED)
            .select_related("owner").order_by("created_at"))
    rows = [{"ext": e, "range": plan.match_range(e.number)} for e in exts]
    return render(request, "portal/orga/queue.html", {"event": event, "rows": rows, "active": "queue"})


@require_orga
@require_POST
def orga_moderate(request, slug, *, event, pk, decision):
    ext = get_object_or_404(Extension, pk=pk, event=event)
    note = (request.POST.get("note") or "").strip()
    try:
        if decision == "approve":
            services.approve(ext, request.user, note=note, request=request)
            messages.success(request, _("%(n)s approved.") % {"n": ext.number})
        elif decision == "reject":
            services.reject(ext, request.user, note=note, request=request)
            messages.success(request, _("%(n)s rejected.") % {"n": ext.number})
        else:
            raise Http404
    except ExtensionError as exc:
        messages.error(request, str(exc))
    return _back(request, reverse("portal:orga_queue", args=[event.slug]))


@require_orga
def orga_extensions(request, slug, *, event):
    qs = Extension.objects.filter(event=event).select_related("owner").annotate(
        device_count=Count("bindings", distinct=True))
    state = request.GET.get("state") or ""
    ext_type = request.GET.get("type") or ""
    q = (request.GET.get("q") or "").strip()
    if state:
        qs = qs.filter(state=state)
    else:
        qs = qs.exclude(state=Extension.State.DELETED)
    if ext_type:
        qs = qs.filter(type=ext_type)
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(display_name__icontains=q) | Q(owner__username__icontains=q)
                       | Q(owner__email__icontains=q) | Q(description__icontains=q))
    page = Paginator(qs.order_by("number"), 100).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "portal/orga/extensions.html", {
        "event": event, "page": page, "state": state, "type": ext_type, "q": q, "active": "extensions",
        "states": Extension.State.choices, "types": ExtensionType.choices,
        "query": params.urlencode(),
    })


@require_orga
@require_POST
def orga_extension_action(request, slug, *, event, pk, action):
    ext = get_object_or_404(Extension, pk=pk, event=event)
    note = (request.POST.get("note") or "").strip()
    try:
        if action == "suspend":
            services.suspend(ext, request.user, note=note, request=request)
        elif action == "reactivate":
            services.reactivate(ext, request.user, request=request)
        elif action == "delete":
            services.delete(ext, request.user, request=request)
        elif action == "provision":
            from apps.extensions.tasks import provision_extension

            provision_extension.delay(str(ext.pk))
            audit(action="provision", actor=request.user, target=ext, event=event, request=request,
                  message="Manual re-provisioning")
        else:
            raise Http404
        messages.success(request, _("%(n)s: %(a)s done.") % {"n": ext.number, "a": action})
    except ExtensionError as exc:
        messages.error(request, str(exc))
    return _back(request, reverse("portal:orga_extensions", args=[event.slug]))


@require_orga
def orga_members(request, slug, *, event):
    add_form = forms.AddMemberForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            add_form = forms.AddMemberForm(request.POST)
            if add_form.is_valid():
                u = add_form.cleaned_data["identifier"]
                m, created = EventMembership.objects.get_or_create(
                    event=event, user=u, defaults={"role": add_form.cleaned_data["role"]})
                if not created:
                    m.role = add_form.cleaned_data["role"]
                    m.save(update_fields=["role", "updated_at"])
                audit(action="create" if created else "update", actor=request.user, target=m, event=event,
                      request=request, message=f"Member {u} as {m.role}")
                messages.success(request, _("%(u)s is now %(r)s.") % {"u": u, "r": m.get_role_display()})
                return redirect("portal:orga_members", event.slug)
        elif action == "update":
            m = get_object_or_404(EventMembership, pk=request.POST.get("pk"), event=event)
            form = forms.MemberUpdateForm(request.POST, instance=m, event=event)
            if form.is_valid():
                old_role = m.role
                form.save()
                audit(action="update", actor=request.user, target=m, event=event, request=request,
                      changes={"role": [old_role, m.role], "groups": list(m.groups.values_list("slug", flat=True))})
                messages.success(request, _("Membership of %(u)s updated.") % {"u": m.user})
            else:
                messages.error(request, _("Invalid membership update."))
            return redirect("portal:orga_members", event.slug)
        elif action == "remove":
            m = get_object_or_404(EventMembership, pk=request.POST.get("pk"), event=event)
            if m.user_id == request.user.pk:
                messages.error(request, _("You cannot remove yourself."))
            else:
                audit(action="delete", actor=request.user, target=m, event=event, request=request,
                      message=f"Removed member {m.user}")
                m.delete()
                messages.success(request, _("Member removed."))
            return redirect("portal:orga_members", event.slug)
    q = (request.GET.get("q") or "").strip()
    memberships = event.memberships.select_related("user").prefetch_related("groups").order_by(
        "role", "user__username")
    if q:
        memberships = memberships.filter(Q(user__username__icontains=q) | Q(user__email__icontains=q)
                                         | Q(user__display_name__icontains=q))
    rows = [{"m": m, "form": forms.MemberUpdateForm(instance=m, event=event, prefix=f"m{m.pk}")}
            for m in memberships[:300]]
    return render(request, "portal/orga/members.html", {
        "event": event, "rows": rows, "add_form": add_form, "q": q, "active": "members",
        "groups": UserGroup.objects.filter(event=event), "roles": EventMembership.Role.choices,
    })


@require_orga
def orga_groups(request, slug, *, event):
    editing = None
    if request.GET.get("edit"):
        editing = get_object_or_404(UserGroup, pk=request.GET["edit"], event=event)
    form = forms.UserGroupForm(instance=editing or UserGroup(event=event), event=event)
    if request.method == "POST":
        if request.POST.get("action") == "delete":
            g = get_object_or_404(UserGroup, pk=request.POST.get("pk"), event=event)
            audit(action="delete", actor=request.user, target=g, event=event, request=request,
                  message=f"Group '{g.name}' deleted")
            g.delete()
            messages.success(request, _("Group deleted."))
            return redirect("portal:orga_groups", event.slug)
        if request.POST.get("pk"):
            editing = get_object_or_404(UserGroup, pk=request.POST["pk"], event=event)
        form = forms.UserGroupForm(request.POST, instance=editing or UserGroup(event=event), event=event)
        if form.is_valid():
            g = form.save()
            audit(action="update" if editing else "create", actor=request.user, target=g, event=event,
                  request=request, message=f"Group '{g.name}' saved")
            messages.success(request, _("Group '%(g)s' saved.") % {"g": g.name})
            return redirect("portal:orga_groups", event.slug)
    groups = UserGroup.objects.filter(event=event).annotate(member_count=Count("members", distinct=True))
    return render(request, "portal/orga/groups.html", {"event": event, "groups": groups, "form": form,
                                                       "editing": editing, "active": "groups"})


def _claim_url(ext):
    return settings.PET_PUBLIC_URL.rstrip("/") + reverse("portal:claim_guest", args=[ext.claim_token])


@require_orga
def orga_guests(request, slug, *, event):
    if not features.enabled("guest_extensions", event):
        raise Http404
    if not event.allow_guest_extensions:
        messages.warning(request, _("Enable guest extensions in the event settings first."))
        return redirect("portal:orga_event_settings", event.slug)
    form = forms.GuestCreateForm(request.POST or None)
    results = []
    if request.method == "POST" and form.is_valid():
        for n in form.cleaned_data["numbers"]:
            try:
                ext = services.create_guest_extension(event, n, request.user, request=request)
                results.append({"number": n, "ok": True, "ext": ext})
            except ExtensionError as exc:
                results.append({"number": n, "ok": False, "error": str(exc)})
        ok = sum(1 for r in results if r["ok"])
        messages.success(request, _("%(ok)d of %(total)d guest extensions created.")
                         % {"ok": ok, "total": len(results)})
    guests = (Extension.objects.filter(event=event, is_temporary=True)
              .exclude(state__in=[Extension.State.DELETED, Extension.State.EXPIRED])
              .select_related("owner").order_by("number"))
    rows = []
    for g in guests:
        row = {"ext": g, "claim_url": _claim_url(g) if g.claim_token else "", "qr": ""}
        if g.claim_token and not g.owner_id:
            row["qr"] = base64.b64encode(qr_png(row["claim_url"], box_size=4)).decode()
        rows.append(row)
    template = "portal/orga/guests_print.html" if request.GET.get("print") else "portal/orga/guests.html"
    return render(request, template, {"event": event, "form": form, "rows": rows, "results": results,
                                      "active": "guests"})


@require_orga
def orga_audit(request, slug, *, event):
    qs = AuditLog.objects.filter(event=event).select_related("actor")
    action = request.GET.get("action") or ""
    q = (request.GET.get("q") or "").strip()
    if action:
        qs = qs.filter(action=action)
    if q:
        qs = qs.filter(Q(message__icontains=q) | Q(target_repr__icontains=q) | Q(actor_repr__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "portal/orga/audit.html", {
        "event": event, "page": page, "action": action, "q": q, "actions": AuditLog.Action.choices,
        "query": params.urlencode(), "active": "audit",
    })


@require_orga
def orga_webhooks(request, slug, *, event):
    if not features.enabled("webhooks", event):
        raise Http404
    editing = None
    if request.GET.get("edit"):
        editing = get_object_or_404(Webhook, pk=request.GET["edit"], event=event)
    form = forms.WebhookForm(instance=editing or Webhook(event=event))
    if request.method == "POST":
        if request.POST.get("action") == "delete":
            wh = get_object_or_404(Webhook, pk=request.POST.get("pk"), event=event)
            audit(action="delete", actor=request.user, target=wh, event=event, request=request,
                  message=f"Webhook '{wh.name}' deleted")
            wh.delete()
            messages.success(request, _("Webhook deleted."))
            return redirect("portal:orga_webhooks", event.slug)
        if request.POST.get("pk"):
            editing = get_object_or_404(Webhook, pk=request.POST["pk"], event=event)
        form = forms.WebhookForm(request.POST, instance=editing or Webhook(event=event))
        if form.is_valid():
            wh = form.save()
            audit(action="update" if editing else "create", actor=request.user, target=wh, event=event,
                  request=request, message=f"Webhook '{wh.name}' saved")
            messages.success(request, _("Webhook saved."))
            return redirect("portal:orga_webhooks", event.slug)
    return render(request, "portal/orga/webhooks.html", {
        "event": event, "webhooks": Webhook.objects.filter(event=event).order_by("name"), "form": form,
        "editing": editing, "active": "webhooks",
    })


@require_orga
def orga_export(request, slug, *, event):
    audit(action="other", actor=request.user, target=event, event=event, request=request, message="Event export")
    return _json_download(export_event(event), f"pet-{event.slug}.json")


@require_orga
def orga_import(request, slug, *, event):
    if not request.user.is_superuser:
        raise PermissionDenied
    form = forms.ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            data = json.load(form.cleaned_data["file"])
            new = import_event(data, slug_override=form.cleaned_data.get("slug_override") or None,
                               actor=request.user)
        except (ValueError, KeyError, TypeError) as exc:
            form.add_error(None, _("Import failed: %(e)s") % {"e": exc})
        else:
            messages.success(request, _("Event '%(n)s' imported.") % {"n": new.name})
            return redirect("portal:orga_dashboard", new.slug)
    return render(request, "portal/orga/import.html", {"event": event, "form": form, "active": "settings"})


@require_orga
def orga_import_csv(request, slug, *, event):
    """Three steps: upload/paste -> preview (CSV carried base64 in a hidden field) -> apply."""
    from apps.extensions import csv_import

    step = request.POST.get("step") if request.method == "POST" else "upload"
    ctx = {"event": event, "active": "extensions", "step": "upload", "form": forms.CSVImportForm()}
    if step in ("preview", "apply"):
        try:
            text = base64.b64decode(request.POST.get("csv_b64", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            text = ""
        create_users = request.POST.get("create_users") == "1"
        try:
            rows = csv_import.parse_csv(text)
        except csv_import.CSVImportError as exc:
            messages.error(request, _("Import failed: %(e)s") % {"e": exc})
            return render(request, "portal/orga/import_csv.html", ctx)
        ctx.update({"csv_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "create_users": create_users})
        if step == "apply":
            result = csv_import.apply(event, rows, request.user, create_users=create_users, request=request)
            if result.applied:
                messages.success(request, _("%(n)d extension(s) imported.") % {"n": result.applied})
            if result.errors:
                messages.warning(request, _("%(n)d row(s) were skipped or failed.") % {"n": len(result.errors)})
            ctx.update({"step": "done", "result": result, "plan": result.plan})
        else:
            ctx.update({"step": "preview",
                        "plan": csv_import.preview(event, rows, create_users=create_users, actor=request.user)})
        return render(request, "portal/orga/import_csv.html", ctx)
    form = forms.CSVImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        text = form.cleaned_data["csv_text"]
        create_users = form.cleaned_data["create_users"]
        try:
            rows = csv_import.parse_csv(text)
        except csv_import.CSVImportError as exc:
            form.add_error(None, _("Import failed: %(e)s") % {"e": exc})
        else:
            ctx.update({"step": "preview", "create_users": create_users,
                        "csv_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                        "plan": csv_import.preview(event, rows, create_users=create_users, actor=request.user)})
            return render(request, "portal/orga/import_csv.html", ctx)
    ctx["form"] = form
    return render(request, "portal/orga/import_csv.html", ctx)


@require_orga
def orga_import_csv_sample(request, slug, *, event):
    from apps.extensions.csv_import import sample_csv

    resp = HttpResponse(sample_csv(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="pet-{event.slug}-extensions-sample.csv"'
    return resp


@require_helpdesk
def helpdesk_lookup(request, slug, *, event):
    form = forms.HelpdeskSearchForm(request.GET or None)
    q = form.cleaned_data.get("q", "").strip() if form.is_valid() else ""
    users: list[User] = []
    if q:
        user_ids = set(User.objects.filter(Q(username__icontains=q) | Q(email__icontains=q)
                                           | Q(display_name__icontains=q)).values_list("pk", flat=True)[:50])
        user_ids |= set(Extension.objects.filter(event=event, owner__isnull=False)
                        .filter(Q(number__startswith=q) | Q(display_name__icontains=q))
                        .exclude(state=Extension.State.DELETED).values_list("owner_id", flat=True)[:50])
        user_ids |= set(Device.objects.filter(event=event, owner__isnull=False)
                        .filter(Q(ipei__startswith=q) | Q(sip_username__icontains=q) | Q(mac_address__icontains=q))
                        .values_list("owner_id", flat=True)[:50])
        users = list(User.objects.filter(pk__in=user_ids).order_by("username")[:50])
    if request.GET.get("user"):
        users = list(User.objects.filter(pk=request.GET["user"]))
    results = []
    for u in users:
        exts = list(_my_extensions(event, u).prefetch_related("bindings__device__last_seen_rfp"))
        devs = list(Device.objects.filter(event=event, owner=u).select_related("last_seen_rfp"))
        results.append({"user": u, "role": u.role_for(event), "groups": u.groups_for(event),
                        "extensions": exts, "devices": devs,
                        "portable": services.portable_extensions(u, event)})
    if q and len(results) == 1 or request.GET.get("user"):
        audit(action="impersonate", actor=request.user, event=event, request=request,
              message=f"Helpdesk viewed {results[0]['user']}" if results else "Helpdesk lookup")
    history = None
    if q and looks_like_number(q):
        history = number_history(event, q, request.user)
        if not history["extensions"]:
            history = None
    return render(request, "portal/orga/helpdesk.html", {"event": event, "form": form, "q": q,
                                                         "results": results, "active": "helpdesk",
                                                         "history": history, "now": timezone.now()})


@require_orga
def orga_service_accounts(request, slug, *, event):
    raw_token = None
    form = forms.ServiceAccountForm()
    if request.method == "POST":
        if request.POST.get("action") == "revoke":
            acct = get_object_or_404(ServiceAccount, pk=request.POST.get("pk"), event=event)
            acct.is_active = False
            acct.save(update_fields=["is_active"])
            audit(action="delete", actor=request.user, target=acct, event=event, request=request,
                  message="Service token revoked")
            messages.success(request, _("Token '%(n)s' revoked.") % {"n": acct.name})
            return redirect("portal:orga_service_accounts", event.slug)
        form = forms.ServiceAccountForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            acct, raw_token = ServiceAccount.issue(name=d["name"], owner=request.user, event=event,
                                                   scopes=d["scopes"], expires_at=d["expires_at"],
                                                   description=d["description"])
            audit(action="create", actor=request.user, target=acct, event=event, request=request,
                  message="Service token created")
            messages.success(request, _("Token created. Copy it now - it will not be shown again."))
            form = forms.ServiceAccountForm()
    accounts = ServiceAccount.objects.filter(event=event).select_related("owner").order_by("-is_active", "name")
    return render(request, "portal/orga/tokens.html", {"event": event, "form": form, "accounts": accounts,
                                                       "raw_token": raw_token, "active": "tokens"})


@require_orga
@require_POST
def orga_resync(request, slug, *, event):
    pbx_count = None
    try:
        from apps.pbx.tasks import sync_event

        res = sync_event.delay(str(event.pk))
        pbx_count = getattr(res, "result", None) if getattr(res, "ready", lambda: False)() else None
    except ImportError:
        from apps.pbx import get_pbx

        pbx_count = get_pbx(event).sync_event(event)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, _("PBX resync failed: %(e)s") % {"e": exc})
    dect_summary = {}
    try:
        from apps.dect.services import sync_infrastructure

        dect_summary = sync_infrastructure(event) or {}
    except Exception as exc:  # noqa: BLE001
        messages.error(request, _("DECT resync failed: %(e)s") % {"e": exc})
    audit(action="provision", actor=request.user, target=event, event=event, request=request,
          message=f"Manual resync: pbx={pbx_count} dect={dect_summary}")
    messages.success(request, _("Resync done. PBX objects: %(p)s · RFPs: %(r)s · handsets: %(h)s") % {
        "p": pbx_count if pbx_count is not None else _("queued"),
        "r": dect_summary.get("rfps", "-"), "h": dect_summary.get("handsets", "-")})
    return redirect("portal:orga_dashboard", event.slug)
