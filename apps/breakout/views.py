"""Breakout portal (orga only): trunks, rules, permissions and usage."""
from django import forms
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.audit import log
from apps.core.features import require
from apps.events.models import UserGroup
from apps.extensions.models import Extension
from apps.portal.shortcuts import require_orga

from . import services
from .models import BreakoutPermission, BreakoutUsage, OutboundRule, Trunk


class TrunkForm(forms.ModelForm):
    class Meta:
        model = Trunk
        fields = ["name", "sip_host", "port", "transport", "auth_user", "auth_password", "from_domain",
                  "outbound_prefix", "caller_id_default", "enabled"]
        widgets = {"auth_password": forms.PasswordInput(render_value=True)}


class RuleForm(forms.ModelForm):
    class Meta:
        model = OutboundRule
        fields = ["trunk", "name", "pattern", "allow", "per_call_max_minutes", "priority"]

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["trunk"].queryset = Trunk.objects.filter(event=event)


class PermissionForm(forms.ModelForm):
    class Meta:
        model = BreakoutPermission
        fields = ["extension", "user_group", "allowed", "daily_minutes_limit"]

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["extension"].queryset = Extension.objects.filter(event=event).active()
        self.fields["user_group"].queryset = UserGroup.objects.filter(event=event)

    def clean(self):
        d = super().clean()
        if not d.get("extension") and not d.get("user_group"):
            raise forms.ValidationError(_("Pick an extension or a user group."))
        return d


def _index(event):
    return redirect(reverse("breakout:index", args=[event.slug]))


@require_orga
@require("breakout")
def index(request, slug, *, event):
    trunks = Trunk.objects.filter(event=event).prefetch_related("rules")
    return render(request, "breakout/index.html", {
        "event": event, "trunks": trunks,
        "rules": OutboundRule.objects.filter(trunk__event=event).select_related("trunk"),
        "permissions": BreakoutPermission.objects.filter(event=event).select_related("extension", "user_group"),
        "usage": BreakoutUsage.objects.filter(event=event).select_related("extension")[:100],
        "rule_form": RuleForm(event=event), "perm_form": PermissionForm(event=event),
        "blocklist": services.blocklist(),
    })


@require_orga
@require("breakout")
def trunk_edit(request, slug, pk=None, *, event):
    trunk = get_object_or_404(Trunk, pk=pk, event=event) if pk else None
    form = TrunkForm(request.POST or None, instance=trunk)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.event = event
        obj.save()
        log(action="update" if trunk else "create", actor=request.user, target=obj, event=event, request=request,
            message=f"Breakout trunk {obj.name}")
        messages.success(request, _("Trunk saved."))
        return _index(event)
    return render(request, "breakout/trunk_form.html", {
        "event": event, "form": form, "trunk": trunk,
        "pjsip": services.pjsip_trunk_config(trunk) if trunk else ""})


@require_orga
@require("breakout")
def trunk_pjsip(request, slug, pk, *, event):
    trunk = get_object_or_404(Trunk, pk=pk, event=event)
    return HttpResponse(services.pjsip_trunk_config(trunk), content_type="text/plain; charset=utf-8")


@require_orga
@require("breakout")
@require_POST
def rule_new(request, slug, *, event):
    form = RuleForm(request.POST, event=event)
    if form.is_valid():
        form.save()
        messages.success(request, _("Rule added."))
    else:
        messages.error(request, _("Invalid rule: %(e)s") % {"e": form.errors.as_text()})
    return _index(event)


@require_orga
@require("breakout")
@require_POST
def rule_delete(request, slug, pk, *, event):
    get_object_or_404(OutboundRule, pk=pk, trunk__event=event).delete()
    return _index(event)


@require_orga
@require("breakout")
@require_POST
def permission_new(request, slug, *, event):
    form = PermissionForm(request.POST, event=event)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.event = event
        obj.save()
        messages.success(request, _("Permission added."))
    else:
        messages.error(request, _("Invalid permission: %(e)s") % {"e": form.errors.as_text()})
    return _index(event)


@require_orga
@require("breakout")
@require_POST
def permission_delete(request, slug, pk, *, event):
    get_object_or_404(BreakoutPermission, pk=pk, event=event).delete()
    return _index(event)
