"""Orga page: connect this event to its venue PBX and DECT system."""
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.audit import log as audit
from apps.dect import get_dect, reset_dect_cache
from apps.dect.models import DECTConnection
from apps.pbx import get_pbx, reset_pbx_cache
from apps.portal.shortcuts import require_orga

from .forms import DECTConnectionForm, PBXConnectionForm
from .models import PBXConnection


def _health(adapter):
    try:
        return adapter.health()
    except Exception as exc:  # noqa: BLE001 - shown to the orga, never a 500
        return {"ok": False, "backend": getattr(adapter, "name", "?"), "error": str(exc)}


@require_orga
def infrastructure(request, slug, *, event):
    pbx_conn = PBXConnection.objects.filter(event=event).first()
    dect_conn = DECTConnection.objects.filter(event=event).first()
    pbx_form = PBXConnectionForm(instance=pbx_conn or PBXConnection(event=event), prefix="pbx")
    dect_form = DECTConnectionForm(instance=dect_conn or DECTConnection(event=event), prefix="dect")
    health = None

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save-pbx":
            pbx_form = PBXConnectionForm(request.POST, instance=pbx_conn or PBXConnection(event=event), prefix="pbx")
            if pbx_form.is_valid():
                conn = pbx_form.save()
                reset_pbx_cache()
                audit(action="update" if pbx_conn else "create", actor=request.user, target=conn, event=event,
                      request=request, message=f"PBX connection: {conn.backend}/{conn.provisioning} "
                                               f"{conn.ari_url or conn.ami_host}")
                messages.success(request, _("PBX connection saved."))
                return redirect("pbx:infrastructure", event.slug)
        elif action == "save-dect":
            dect_form = DECTConnectionForm(request.POST, instance=dect_conn or DECTConnection(event=event),
                                           prefix="dect")
            if dect_form.is_valid():
                conn = dect_form.save()
                reset_dect_cache()
                audit(action="update" if dect_conn else "create", actor=request.user, target=conn, event=event,
                      request=request, message=f"DECT connection: {conn.backend} {conn.host}")
                messages.success(request, _("DECT connection saved."))
                return redirect("pbx:infrastructure", event.slug)
        elif action == "reset-pbx" and pbx_conn is not None:
            audit(action="delete", actor=request.user, target=pbx_conn, event=event, request=request,
                  message="PBX connection removed (server default)")
            pbx_conn.delete()
            reset_pbx_cache()
            messages.success(request, _("PBX connection removed - this event now uses the server default."))
            return redirect("pbx:infrastructure", event.slug)
        elif action == "reset-dect" and dect_conn is not None:
            audit(action="delete", actor=request.user, target=dect_conn, event=event, request=request,
                  message="DECT connection removed (server default)")
            dect_conn.delete()
            reset_dect_cache()
            messages.success(request, _("DECT connection removed - this event now uses the server default."))
            return redirect("pbx:infrastructure", event.slug)
        elif action == "test":
            health = {"pbx": _health(get_pbx(event)), "dect": _health(get_dect(event))}

    pbx = get_pbx(event)
    dect = get_dect(event)
    public = settings.PET_PUBLIC_URL.rstrip("/")
    agent = None
    if pbx_conn is not None and pbx_conn.is_agent:
        from apps.pbx.snapshot import agent_state

        agent = agent_state(pbx_conn, event)
        agent["secret"] = pbx_conn.hook_secret  # blank -> the template asks to set one (server secret stays hidden)
    return render(request, "pbx/infrastructure.html", {
        "event": event, "pbx_form": pbx_form, "dect_form": dect_form, "pbx_conn": pbx_conn, "dect_conn": dect_conn,
        "pbx_backend": pbx.name, "dect_backend": dect.name, "health": health, "public_url": public,
        "hook_url": public + reverse("api:pbx-hook", args=["cdr"]).rsplit("cdr/", 1)[0],
        "route_url": public + reverse("api:pbx-route"),
        "dialplan_url": public + reverse("api:pbx-dialplan"),
        "snapshot_url": public + reverse("api:pbx-snapshot"),
        "schema_url": public + reverse("api:pbx-snapshot-schema"),
        "heartbeat_url": public + reverse("api:pbx-agent-heartbeat"),
        "agent": agent,
        "default_pbx": settings.PET_PBX_BACKEND.rsplit(".", 1)[-1],
        "default_dect": settings.PET_DECT_BACKEND.rsplit(".", 1)[-1],
        "sip_domain": event.sip_domain or settings.ASTERISK.get("SIP_DOMAIN", ""),
    })
