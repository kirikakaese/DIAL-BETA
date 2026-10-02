"""Emergency portal (orga only): targets, incidents, broadcast, priorities."""
from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.features import require
from apps.events.models import UserGroup
from apps.extensions.models import Extension
from apps.portal.shortcuts import require_orga

from . import services
from .models import BroadcastAnnouncement, EmergencyIncident, EmergencyTarget
from .services import EmergencyError


class TargetForm(forms.Form):
    number = forms.ChoiceField(label=_("Emergency number"))
    label = forms.CharField(label=_("Label"), max_length=80, required=False)
    destination_extension = forms.ModelChoiceField(label=_("Destination extension / group"),
                                                   queryset=Extension.objects.none(), required=False)
    fallback_number = forms.CharField(label=_("Fallback number"), max_length=32, required=False)
    announce_location = forms.BooleanField(label=_("Announce caller location"), required=False, initial=True)
    priority = forms.IntegerField(label=_("Priority"), min_value=0, max_value=100, initial=100)

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["number"].choices = [(n, n) for n in services.emergency_numbers(event)]
        self.fields["destination_extension"].queryset = Extension.objects.filter(event=event).active()


class BroadcastForm(forms.Form):
    announcement = forms.CharField(label=_("Announcement (TTS text or audio path on the PBX)"),
                                   widget=forms.Textarea(attrs={"rows": 3}))
    group = forms.ModelChoiceField(label=_("Only this group (blank = everyone)"), queryset=UserGroup.objects.none(),
                                   required=False)
    confirm = forms.BooleanField(label=_("I understand this rings every handset"))

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].queryset = UserGroup.objects.filter(event=event)


def _index(event):
    return redirect(reverse("emergency:index", args=[event.slug]))


@require_orga
@require("emergency")
def index(request, slug, *, event):
    return render(request, "emergency/index.html", {
        "event": event,
        "targets": services.targets_for(event),
        "numbers": services.emergency_numbers(event),
        "incidents": EmergencyIncident.objects.filter(event=event)
                     .select_related("caller_extension", "handled_by")[:50],
        "broadcasts": BroadcastAnnouncement.objects.filter(event=event).select_related("sent_by")[:10],
        "candidates": services.preempt_candidates(event)[:100],
        "target_form": TargetForm(event=event),
        "broadcast_form": BroadcastForm(event=event),
    })


@require_orga
@require("emergency")
@require_POST
def target_save(request, slug, *, event):
    form = TargetForm(request.POST, event=event)
    if form.is_valid():
        d = dict(form.cleaned_data)
        try:
            services.create_target(event, request.user, d.pop("number"), request=request, **d)
            messages.success(request, _("Emergency target saved."))
        except EmergencyError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, _("Invalid target: %(e)s") % {"e": form.errors.as_text()})
    return _index(event)


@require_orga
@require("emergency")
@require_POST
def target_delete(request, slug, pk, *, event):
    get_object_or_404(EmergencyTarget, pk=pk, event=event).delete()
    messages.success(request, _("Emergency target removed."))
    return _index(event)


@require_orga
@require("emergency")
@require_POST
def incident_resolve(request, slug, pk, *, event):
    inc = get_object_or_404(EmergencyIncident, pk=pk, event=event)
    services.resolve_incident(inc, request.user, notes=request.POST.get("notes", ""), request=request)
    messages.success(request, _("Incident resolved."))
    return _index(event)


@require_orga
@require("emergency")
@require_POST
def broadcast(request, slug, *, event):
    form = BroadcastForm(request.POST, event=event)
    if form.is_valid():
        try:
            ba = services.broadcast_all(event, request.user, form.cleaned_data["announcement"],
                                        group=form.cleaned_data["group"], request=request)
            messages.success(request, _("Emergency broadcast started on %(n)s handsets.") % {"n": ba.targets})
        except EmergencyError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, _("Please enter an announcement and confirm."))
    return _index(event)


@require_orga
@require("emergency")
@require_POST
def priority(request, slug, pk, *, event):
    ext = get_object_or_404(Extension, pk=pk, event=event)
    try:
        services.set_priority(ext, int(request.POST.get("level", 0)), request.user, request=request)
        messages.success(request, _("Priority of %(n)s set to %(p)s.") % {"n": ext.number, "p": ext.priority})
    except ValueError:
        messages.error(request, _("Priority must be a number."))
    return _index(event)
