"""Messaging portal: inbox/outbox + send form; orga broadcast."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.features import require
from apps.events.models import UserGroup
from apps.extensions.models import Extension
from apps.portal.shortcuts import require_orga, with_event

from . import services
from .models import MAX_TEXT, Broadcast, Message
from .services import MessagingError


class SendForm(forms.Form):
    number = forms.CharField(label=_("Extension number"), max_length=16)
    text = forms.CharField(label=_("Text"), max_length=MAX_TEXT, widget=forms.Textarea(attrs={"rows": 3}))


class BroadcastForm(forms.Form):
    target = forms.ChoiceField(label=_("Target"), choices=Broadcast.Target.choices)
    group = forms.ModelChoiceField(label=_("Group"), queryset=UserGroup.objects.none(), required=False)
    text = forms.CharField(label=_("Text"), max_length=MAX_TEXT, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].queryset = UserGroup.objects.filter(event=event)

    def clean(self):
        d = super().clean()
        if d.get("target") == Broadcast.Target.GROUP and not d.get("group"):
            self.add_error("group", _("Pick a group."))
        return d


def _my_messages(event, user):
    return (Message.objects.filter(event=event)
            .filter(Q(sender=user) | Q(recipient_extension__owner=user) | Q(sender_extension__owner=user))
            .select_related("sender", "recipient_extension", "sender_extension"))


@login_required
@with_event
@require("messaging")
def index(request, slug, *, event):
    form = SendForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        number = form.cleaned_data["number"].strip()
        ext = Extension.objects.filter(event=event, number=number).active().first()
        if ext is None:
            form.add_error("number", _("Unknown or inactive extension."))
        else:
            try:
                msg = services.send_to_extension(event, request.user, ext, form.cleaned_data["text"])
                if msg is not None and msg.state == Message.State.SENT:
                    messages.success(request, _("Message sent to %(n)s.") % {"n": number})
                else:
                    messages.error(request, _("Message could not be delivered: %(e)s")
                                   % {"e": getattr(msg, "error", "")})
                return redirect(reverse("messaging:index", args=[event.slug]))
            except MessagingError as exc:
                form.add_error(None, str(exc))
    mine = _my_messages(event, request.user)
    return render(request, "messaging/index.html", {
        "event": event, "form": form, "max_text": MAX_TEXT,
        "inbox": mine.filter(recipient_extension__owner=request.user)[:30],
        "outbox": mine.filter(sender=request.user)[:30],
    })


@require_orga
@require("messaging")
def broadcast(request, slug, *, event):
    form = BroadcastForm(request.POST or None, event=event)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            bc = services.broadcast(event, request.user, d["text"],
                                    group=d["group"] if d["target"] == Broadcast.Target.GROUP else None,
                                    request=request)
            messages.success(request, _("Broadcast sent: %(ok)s delivered, %(bad)s failed.")
                             % {"ok": bc.sent_count, "bad": bc.failed_count})
            return redirect(reverse("messaging:broadcast", args=[event.slug]))
        except MessagingError as exc:
            form.add_error(None, str(exc))
    return render(request, "messaging/broadcast.html", {
        "event": event, "form": form, "max_text": MAX_TEXT,
        "history": Broadcast.objects.filter(event=event).select_related("sender", "group")[:20],
    })
