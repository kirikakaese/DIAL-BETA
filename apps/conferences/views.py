"""Conference portal: list, create, live detail with kick."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.features import require
from apps.extensions.services import ExtensionError
from apps.portal.shortcuts import with_event

from . import services
from .models import ConferenceParticipant, ConferenceRoom
from .services import ConferenceError


class RoomForm(forms.Form):
    number = forms.CharField(label=_("Extension number"), max_length=16)
    name = forms.CharField(label=_("Room name"), max_length=80, required=False)
    pin = forms.CharField(label=_("PIN (4-8 digits, blank = open)"), max_length=8, required=False)
    max_participants = forms.IntegerField(label=_("Max participants"), min_value=2, max_value=200, initial=20)
    record = forms.BooleanField(label=_("Record"), required=False)
    music_on_hold = forms.BooleanField(label=_("Music on hold when alone"), required=False, initial=True)
    announce_join = forms.BooleanField(label=_("Announce join/leave"), required=False, initial=True)
    is_public = forms.BooleanField(label=_("Listed publicly"), required=False, initial=True)


def _visible_room(request, event, pk):
    room = get_object_or_404(ConferenceRoom.objects.select_related("extension", "owner"), pk=pk,
                             extension__event=event)
    if not (room.is_public or room.owner_id == request.user.pk or request.user.is_orga(event)):
        raise PermissionDenied
    return room


def _can_manage(request, event, room):
    return room.owner_id == request.user.pk or request.user.is_orga(event)


@login_required
@with_event
@require("conferences")
def index(request, slug, *, event):
    rooms = services.visible_rooms(event, request.user)
    return render(request, "conferences/index.html", {
        "event": event, "my_rooms": rooms.filter(owner=request.user),
        "public_rooms": rooms.filter(is_public=True).exclude(owner=request.user),
    })


@login_required
@with_event
@require("conferences")
def new(request, slug, *, event):
    form = RoomForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            room = services.create_room(event, request.user, d.pop("number").strip(), d.pop("pin"), request=request,
                                        **d)
            messages.success(request, _("Conference room %(n)s created.") % {"n": room.number})
            return redirect(reverse("conferences:detail", args=[event.slug, room.pk]))
        except (ConferenceError, ExtensionError) as exc:
            form.add_error(None, str(exc))
    return render(request, "conferences/form.html", {"event": event, "form": form})


@login_required
@with_event
@require("conferences")
def detail(request, slug, pk, *, event):
    room = _visible_room(request, event, pk)
    can_manage = _can_manage(request, event, room)
    form = None
    if can_manage:
        initial = {k: getattr(room, k) for k in services.ROOM_FIELDS}
        initial["number"] = room.number
        form = RoomForm(request.POST or None, initial=initial)
        form.fields["number"].disabled = True
        if request.method == "POST" and form.is_valid():
            d = dict(form.cleaned_data)
            d.pop("number", None)
            try:
                services.update_room(room, actor=request.user, request=request, **d)
                messages.success(request, _("Room updated."))
                return redirect(reverse("conferences:detail", args=[event.slug, room.pk]))
            except ConferenceError as exc:
                form.add_error(None, str(exc))
    participants = services.refresh_participants(room)
    return render(request, "conferences/detail.html", {"event": event, "room": room, "form": form,
                                                       "participants": participants, "can_manage": can_manage})


@login_required
@with_event
@require("conferences")
@require_POST
def kick(request, slug, pk, participant_pk, *, event):
    room = _visible_room(request, event, pk)
    if not _can_manage(request, event, room):
        raise PermissionDenied
    p = get_object_or_404(ConferenceParticipant, pk=participant_pk, room=room)
    services.kick(p, actor=request.user, request=request)
    messages.success(request, _("Participant %(n)s kicked.") % {"n": p.caller_number})
    return redirect(reverse("conferences:detail", args=[event.slug, room.pk]))
