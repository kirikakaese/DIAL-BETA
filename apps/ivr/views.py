"""IVR portal: my announcements & menus, create/edit forms with an options formset."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.audit import log as audit
from apps.core.features import require
from apps.extensions.services import ExtensionError, get_plan
from apps.portal.shortcuts import with_event

from . import services
from .models import IVR_ACTIONS, Announcement, IVRMenu
from .services import IVRError


class AnnouncementForm(forms.Form):
    number = forms.CharField(label=_("Extension number"), max_length=16)
    tts_text = forms.CharField(label=_("Text to speak (TTS)"), required=False, widget=forms.Textarea(attrs={"rows": 3}))
    audio = forms.FileField(label=_("Audio file (wav/gsm)"), required=False)
    language = forms.CharField(label=_("Language"), max_length=8, initial="en")
    loop = forms.BooleanField(label=_("Loop"), required=False)


class MenuForm(forms.Form):
    number = forms.CharField(label=_("Extension number"), max_length=16)
    prompt_tts = forms.CharField(label=_("Prompt text (TTS)"), required=False, widget=forms.Textarea(attrs={"rows": 3}))
    prompt_audio = forms.FileField(label=_("Prompt audio"), required=False)
    language = forms.CharField(label=_("Language"), max_length=8, initial="en")
    timeout = forms.IntegerField(label=_("Digit timeout (s)"), min_value=1, max_value=60, initial=5)
    invalid_retries = forms.IntegerField(label=_("Invalid retries"), min_value=0, max_value=9, initial=3)


class OptionForm(forms.Form):
    digit = forms.ChoiceField(choices=[(d, d) for d in "1234567890*#"])
    action = forms.ChoiceField(choices=[(a, a) for a in IVR_ACTIONS])
    target = forms.CharField(max_length=16, required=False)
    label = forms.CharField(max_length=60, required=False)


OptionFormSet = formset_factory(OptionForm, extra=2, can_delete=True)


def _own(request, event, obj):
    if obj.extension.event_id != event.pk:
        raise PermissionDenied
    if not (obj.extension.owner_id == request.user.pk or request.user.is_orga(event)):
        raise PermissionDenied
    return obj


@login_required
@with_event
@require("ivr")
def index(request, slug, *, event):
    qs_filter = {} if request.user.is_orga(event) else {"extension__owner": request.user}
    return render(request, "ivr/index.html", {
        "event": event,
        "announcements": Announcement.objects.filter(extension__event=event, **qs_filter).select_related("extension"),
        "menus": IVRMenu.objects.filter(extension__event=event, **qs_filter).select_related("extension"),
    })


@login_required
@with_event
@require("ivr")
def announcement_edit(request, slug, pk=None, *, event):
    ann = _own(request, event, get_object_or_404(Announcement, pk=pk)) if pk else None
    initial = ({"number": ann.extension.number, "tts_text": ann.tts_text, "language": ann.language, "loop": ann.loop}
               if ann else {})
    form = AnnouncementForm(request.POST or None, request.FILES or None, initial=initial)
    if ann:
        form.fields["number"].disabled = True
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            if ann:
                fields = {"tts_text": d["tts_text"], "language": d["language"], "loop": d["loop"]}
                if d.get("audio"):
                    fields["audio"] = d["audio"]
                services.update_announcement(ann, **fields)
            else:
                services.create_announcement(event, request.user, d["number"].strip(), d["tts_text"], d.get("audio"),
                                             language=d["language"], loop=d["loop"], request=request)
            messages.success(request, _("Announcement saved."))
            return redirect(reverse("ivr:index", args=[event.slug]))
        except (IVRError, ExtensionError) as exc:
            form.add_error(None, str(exc))
    ctx = {"event": event, "form": form, "obj": ann}
    if ann:
        plan = get_plan(event)
        ctx.update({"record_number": (plan.announcement_record_number or "").strip(),
                    "record_code": services.ensure_record_code(ann.extension) if services.recording_enabled(plan)
                    else "", "record_dial": services.record_dial_string(plan, ann.extension),
                    "phone_recording": (ann.extension.config or {}).get(services.PHONE_RECORDING_KEY)})
    return render(request, "ivr/announcement_form.html", ctx)


@login_required
@with_event
@require("ivr")
@require_POST
def announcement_new_record_code(request, slug, pk, *, event):
    """Mint a new recording code (invalidates the old one)."""
    ann = _own(request, event, get_object_or_404(Announcement, pk=pk))
    ann.extension.issue_record_code()
    audit(action="update", actor=request.user, target=ann.extension, event=event, request=request,
          message="New announcement recording code issued")
    messages.success(request, _("New recording code issued. The old code no longer works."))
    return redirect(reverse("ivr:announcement_edit", args=[event.slug, ann.pk]))


@login_required
@with_event
@require("ivr")
def menu_edit(request, slug, pk=None, *, event):
    menu = _own(request, event, get_object_or_404(IVRMenu, pk=pk)) if pk else None
    initial = ({"number": menu.extension.number, "prompt_tts": menu.prompt_tts, "language": menu.language,
                "timeout": menu.timeout, "invalid_retries": menu.invalid_retries} if menu else {})
    form = MenuForm(request.POST or None, request.FILES or None, initial=initial)
    fs = OptionFormSet(request.POST or None, initial=menu.options if menu else None, prefix="opt")
    if menu:
        form.fields["number"].disabled = True
    if request.method == "POST" and form.is_valid() and fs.is_valid():
        d = form.cleaned_data
        options = [f.cleaned_data for f in fs.forms
                   if f.cleaned_data and not f.cleaned_data.get("DELETE") and f.cleaned_data.get("digit")]
        options = [{k: o.get(k, "") for k in ("digit", "action", "target", "label")} for o in options]
        try:
            if menu:
                fields = {"prompt_tts": d["prompt_tts"], "language": d["language"], "timeout": d["timeout"],
                          "invalid_retries": d["invalid_retries"], "options": options}
                if d.get("prompt_audio"):
                    fields["prompt_audio"] = d["prompt_audio"]
                services.update_menu(menu, **fields)
            else:
                services.create_menu(event, request.user, d["number"].strip(), options, prompt_tts=d["prompt_tts"],
                                     prompt_audio=d.get("prompt_audio"), language=d["language"],
                                     timeout=d["timeout"], invalid_retries=d["invalid_retries"], request=request)
            messages.success(request, _("Menu saved."))
            return redirect(reverse("ivr:index", args=[event.slug]))
        except (IVRError, ExtensionError) as exc:
            form.add_error(None, str(exc))
    return render(request, "ivr/menu_form.html", {"event": event, "form": form, "formset": fs, "obj": menu})
