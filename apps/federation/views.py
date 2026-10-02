"""Federation portal (orga only): peers, pjsip config, directory."""
from django import forms
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.features import require
from apps.portal.shortcuts import require_orga

from . import services
from .models import FederationDirectoryEntry, FederationPeer
from .services import FederationError


class PeerForm(forms.ModelForm):
    class Meta:
        model = FederationPeer
        fields = ["name", "remote_prefix", "sip_host", "sip_port", "transport", "srtp", "auth_user", "auth_password",
                  "remote_event_name", "directory_url", "state"]
        widgets = {"auth_password": forms.PasswordInput(render_value=True)}


@require_orga
@require("federation")
def index(request, slug, *, event):
    return render(request, "federation/index.html", {
        "event": event,
        "peers": FederationPeer.objects.filter(event=event),
        "directory": FederationDirectoryEntry.objects.all()[:100],
        "own_entry": services.publish_directory_entry(event),
    })


@require_orga
@require("federation")
def peer_edit(request, slug, pk=None, *, event):
    peer = get_object_or_404(FederationPeer, pk=pk, event=event) if pk else None
    form = PeerForm(request.POST or None, instance=peer)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            if peer:
                services.update_peer(peer, actor=request.user, request=request, **d)
            else:
                peer = services.register_peer(event, request.user, request=request, **d)
            messages.success(request, _("Peer %(n)s saved.") % {"n": peer.name})
            return redirect(reverse("federation:index", args=[event.slug]))
        except FederationError as exc:
            form.add_error(None, str(exc))
    return render(request, "federation/peer_form.html", {"event": event, "form": form, "peer": peer,
                                                         "pjsip": services.pjsip_trunk_config(peer) if peer else ""})


@require_orga
@require("federation")
def peer_pjsip(request, slug, pk, *, event):
    peer = get_object_or_404(FederationPeer, pk=pk, event=event)
    return HttpResponse(services.pjsip_trunk_config(peer), content_type="text/plain; charset=utf-8")


@require_orga
@require("federation")
@require_POST
def directory_fetch(request, slug, *, event):
    url = (request.POST.get("url") or "").strip()
    n = len(services.fetch_directory(url)) if url else 0
    messages.success(request, _("%(n)s directory entries fetched.") % {"n": n})
    return redirect(reverse("federation:index", args=[event.slug]))
