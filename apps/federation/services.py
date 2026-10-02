"""Federation services.

PBX contract: ``route(event, number) -> str | None`` returns an Asterisk dial string
(``PJSIP/<rest>@fed-<peer.pk>``) when ``number`` starts with an active peer's ``remote_prefix``;
``None`` when the flag is off or nothing matches.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.audit import log
from apps.core.features import enabled

from .models import FederationDirectoryEntry, FederationPeer

logger = logging.getLogger("pet.federation")


class FederationError(Exception):
    pass


def peers_for(event, active_only: bool = True):
    qs = FederationPeer.objects.filter(event=event)
    if active_only:
        qs = qs.filter(state=FederationPeer.State.ACTIVE)
    return qs.order_by("-remote_prefix")  # longest prefix first


def route(event, number: str) -> str | None:
    if not enabled("federation", event) or not number:
        return None
    number = str(number).strip()
    for peer in peers_for(event):
        if peer.remote_prefix and number.startswith(peer.remote_prefix) and len(number) > len(peer.remote_prefix):
            rest = number[len(peer.remote_prefix):]
            return f"PJSIP/{rest}@{peer.endpoint_name}"
    return None


def register_peer(event, actor, *, name: str, remote_prefix: str, sip_host: str, request=None,
                  **fields) -> FederationPeer:
    if not enabled("federation", event):
        raise FederationError(_("Federation is disabled for this event."))
    remote_prefix = (remote_prefix or "").strip()
    if not remote_prefix.isdigit():
        raise FederationError(_("The remote prefix must consist of digits."))
    if FederationPeer.objects.filter(event=event, remote_prefix=remote_prefix).exists():
        raise FederationError(_("A peer with this prefix already exists."))
    peer = FederationPeer.objects.create(event=event, name=name, remote_prefix=remote_prefix, sip_host=sip_host,
                                         **fields)
    log(action="create", actor=actor, target=peer, event=event, request=request,
        message=f"Federation peer {peer.name} ({peer.remote_prefix})")
    return peer


def update_peer(peer: FederationPeer, actor=None, request=None, **fields) -> FederationPeer:
    changes = {}
    for k, v in fields.items():
        if getattr(peer, k) != v:
            changes[k] = ["***" if k == "auth_password" else getattr(peer, k), "***" if k == "auth_password" else v]
            setattr(peer, k, v)
    if changes:
        peer.save()
        log(action="update", actor=actor, target=peer, event=peer.event, request=request, changes=changes)
    return peer


def mark_seen(peer: FederationPeer) -> None:
    peer.last_seen = timezone.now()
    peer.save(update_fields=["last_seen", "updated_at"])


# --------------------------------------------------------------------------- directory

def publish_directory_entry(event) -> dict:
    """What this instance publishes about ``event`` at ``/api/v1/federation/directory/``."""
    base = getattr(settings, "PET_PUBLIC_URL", "") or ""
    asterisk = getattr(settings, "ASTERISK", {}) or {}
    return {
        "instance_url": base,
        "event_name": event.name,
        "event_slug": event.slug,
        "dial_prefix": event.dial_prefix,
        "sip_host": event.sip_domain or asterisk.get("SIP_DOMAIN", ""),
        "sip_port": int(getattr(settings, "PET_FEDERATION_SIP_PORT", 5061)),
        "transport": "tls",
        "srtp": True,
        "contact": getattr(settings, "PET_FEDERATION_CONTACT", ""),
        "start_date": event.start_date.isoformat() if event.start_date else None,
        "end_date": event.end_date.isoformat() if event.end_date else None,
    }


def fetch_directory(url: str, timeout: int = 10) -> list[FederationDirectoryEntry]:
    """Fetch another instance's directory and refresh the local cache. Network errors -> ``[]``."""
    import requests

    try:
        resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("federation directory fetch %s failed: %s", url, exc)
        return []
    rows = data.get("results", data) if isinstance(data, dict) else data
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("event_name"):
            continue
        entry, _c = FederationDirectoryEntry.objects.update_or_create(
            instance_url=row.get("instance_url") or url, event_slug=row.get("event_slug") or "",
            defaults={"event_name": row["event_name"], "dial_prefix": row.get("dial_prefix") or "",
                      "sip_host": row.get("sip_host") or "", "sip_port": int(row.get("sip_port") or 5061),
                      "contact": row.get("contact") or ""},
        )
        out.append(entry)
    return out


# --------------------------------------------------------------------------- pjsip

def pjsip_trunk_config(peer: FederationPeer) -> str:
    """pjsip.conf snippet for the trunk to ``peer`` (TLS + SRTP by default)."""
    name = peer.endpoint_name
    encryption = "sdes" if peer.srtp else "no"
    if peer.transport == FederationPeer.Transport.WSS:
        encryption = "dtls" if peer.srtp else "no"
    lines = [
        f"; PET federation peer: {peer.name} (prefix {peer.remote_prefix})",
        f"[{name}]",
        "type=endpoint",
        f"transport=transport-{peer.transport}",
        "context=pet-federation-in",
        "disallow=all",
        "allow=opus,g722,alaw,ulaw",
        f"aors={name}",
        f"media_encryption={encryption}",
        "media_encryption_optimistic=no" if peer.srtp else "media_encryption_optimistic=yes",
        "rtp_symmetric=yes",
        "force_rport=yes",
        "rewrite_contact=yes",
        "direct_media=no",
        "trust_id_inbound=yes",
        "send_pai=yes",
        "",
        f"[{name}]",
        "type=aor",
        f"contact=sip:{peer.sip_host}:{peer.sip_port};transport={peer.transport}",
        "qualify_frequency=60",
        "",
        f"[{name}]",
        "type=identify",
        f"endpoint={name}",
        f"match={peer.sip_host}",
    ]
    if peer.auth_user:
        lines[lines.index(f"aors={name}"):lines.index(f"aors={name}")] = [f"outbound_auth={name}-auth"]
        lines += ["", f"[{name}-auth]", "type=auth", "auth_type=userpass", f"username={peer.auth_user}",
                  f"password={peer.auth_password}"]
    return "\n".join(lines) + "\n"
