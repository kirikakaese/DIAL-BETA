"""Portal views: my mailboxes + messages (with playback), per-mailbox settings, orga counts overview."""
from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.features import require
from apps.extensions.models import Extension
from apps.extensions.services import get_plan
from apps.portal.shortcuts import require_orga, with_event

from . import services
from .forms import MailboxSettingsForm
from .models import Mailbox, Message
from .services import VoicemailError


def _index_url(event):
    return reverse("voicemail:index", args=[event.slug])


def _visible_message(request, event, pk) -> Message:
    msg = get_object_or_404(Message.objects.select_related("mailbox__extension__owner", "mailbox__event"),
                            pk=pk, mailbox__event=event)
    if not services.can_access(request.user, msg.mailbox):
        raise PermissionDenied
    return msg


@login_required
@with_event
@require("voicemail")
def index(request, slug, *, event):
    boxes = services.mailboxes_for(request.user, event)
    msgs = (Message.objects.filter(mailbox__in=boxes).select_related("mailbox__extension")
            .order_by("is_read", "-received_at")[:200])
    return render(request, "voicemail/index.html", {
        "event": event, "mailboxes": boxes, "messages_list": msgs, "plan": get_plan(event),
        "unread_total": sum(b.unread_count() for b in boxes),
    })


@login_required
@with_event
@require("voicemail")
def settings_view(request, slug, ext_pk, *, event):
    ext = get_object_or_404(Extension.objects.select_related("owner"), pk=ext_pk, event=event)
    if not (ext.owner_id == request.user.pk or request.user.is_orga(event)):
        raise PermissionDenied
    if ext.type not in services.MAILBOX_TYPES or not ext.is_active:
        raise Http404("extension has no mailbox")
    mailbox = services.ensure_mailbox(ext)
    form = MailboxSettingsForm(request.POST or None, request.FILES or None, instance=mailbox)
    if request.method == "POST" and form.is_valid():
        try:
            # the ModelForm already mutated ``mailbox``; diff/save/sync via the service on a fresh copy
            services.update_mailbox(Mailbox.objects.get(pk=mailbox.pk), actor=request.user, request=request,
                                    **{k: form.cleaned_data[k] for k in form.changed_data})
            flash.success(request, _("Mailbox settings saved."))
            return redirect(_index_url(event))
        except VoicemailError as exc:
            form.add_error(None, str(exc))
    return render(request, "voicemail/settings.html", {"event": event, "mailbox": mailbox, "form": form,
                                                       "extension": ext, "plan": get_plan(event)})


@login_required
@with_event
@require("voicemail")
def audio(request, slug, pk, *, event):
    msg = _visible_message(request, event, pk)
    if not msg.has_audio:
        raise Http404("no audio")
    fh = msg.audio.open("rb")
    resp = FileResponse(fh, content_type=msg.content_type)
    resp["Content-Disposition"] = f'inline; filename="voicemail-{msg.mailbox.number}-{msg.pk}{_ext(msg)}"'
    resp["Cache-Control"] = "private, no-store"
    return resp


def _ext(msg):
    name = msg.audio.name or ""
    return name[name.rfind("."):] if "." in name else ""


@login_required
@with_event
@require("voicemail")
@require_POST
def mark_read(request, slug, pk, *, event):
    msg = _visible_message(request, event, pk)
    services.mark_read(msg, request.POST.get("unread") != "1")
    return redirect(request.POST.get("next") or _index_url(event))


@login_required
@with_event
@require("voicemail")
@require_POST
def delete(request, slug, pk, *, event):
    msg = _visible_message(request, event, pk)
    services.delete_message(msg, actor=request.user, request=request)
    flash.success(request, _("Message deleted."))
    return redirect(request.POST.get("next") or _index_url(event))


@require_orga
@require("voicemail")
def all_view(request, slug, *, event):
    return render(request, "voicemail/all.html", {"event": event, "summary": services.event_summary(event)})
