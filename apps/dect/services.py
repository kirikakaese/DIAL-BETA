"""DECT monitoring services: mirror OMM state into PET, raise/resolve alerts, site survey, coverage.

``sync_infrastructure(event)`` is called by the ``poll_infrastructure`` beat task every 30 s for
every event in state *registration* or *live*. It is idempotent and safe to call from a view
("Sync now").
"""
from __future__ import annotations

import logging
from collections import defaultdict

from django.conf import settings
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.dect import get_dect
from apps.dect.models import RFP, Alert, RFPStatusSample, SiteSurveyLog, SyncCluster
from apps.devices.models import Device
from apps.events.models import Event
from apps.events.webhooks import emit

log = logging.getLogger(__name__)

POLLED_STATES = (Event.State.REGISTRATION, Event.State.LIVE)


def polled_events():
    return Event.objects.filter(state__in=POLLED_STATES)


# --- alerts ----------------------------------------------------------------------

def open_alert(event, kind: str, severity: str, message: str, rfp=None, *, notify=True) -> Alert:
    """Create an alert unless an identical unresolved one already exists (returns that one)."""
    existing = Alert.objects.filter(event=event, kind=kind, rfp=rfp, resolved_at__isnull=True).first()
    if existing:
        return existing
    alert = Alert.objects.create(event=event, kind=kind, severity=severity, message=message[:300], rfp=rfp)
    if notify:
        try:
            notify_alert(alert)
        except Exception:  # noqa: BLE001 - alerting must never break the poll loop
            log.exception("alert notification failed for %s", alert)
    return alert


def resolve_alerts(event, kind: str, rfp=None) -> int:
    qs = Alert.objects.filter(event=event, kind=kind, resolved_at__isnull=True)
    if rfp is not None:
        qs = qs.filter(rfp=rfp)
    return qs.update(resolved_at=timezone.now())


def resolve_alert(alert: Alert):
    if alert.resolved_at is None:
        alert.resolved_at = timezone.now()
        alert.save(update_fields=["resolved_at", "updated_at"])


def notify_alert(alert: Alert) -> None:
    """Fan an alert out to webhooks, ntfy and e-mail (whatever is configured), then mark it."""
    cfg = getattr(settings, "ALERTING", {}) or {}
    payload = {
        "id": alert.pk, "kind": alert.kind, "severity": alert.severity, "message": alert.message,
        "rfp": alert.rfp.name if alert.rfp else None, "rfp_id": alert.rfp.omm_id if alert.rfp else None,
        "event": alert.event.slug, "at": alert.created_at.isoformat() if alert.created_at else None,
    }
    emit(f"dect.{alert.kind}", payload, event=alert.event)

    title = f"[PET/{alert.event.slug}] {alert.get_severity_display()}: {alert.kind}"
    priority = {"critical": "urgent", "warning": "high", "info": "default"}.get(alert.severity, "default")
    ntfy_url = cfg.get("NTFY_URL")
    if ntfy_url:
        import requests

        try:
            requests.post(ntfy_url, data=alert.message.encode("utf-8"),
                          headers={"Title": title, "Priority": priority, "Tags": "telephone"}, timeout=5)
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy notification failed: %s", exc)

    emails = cfg.get("EMAILS")
    if isinstance(emails, str):
        emails = [e.strip() for e in emails.split(",") if e.strip()]
    if emails and alert.severity != Alert.Severity.INFO:
        from django.core.mail import send_mail

        try:
            send_mail(title, alert.message, None, list(emails), fail_silently=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert e-mail failed: %s", exc)

    alert.notified = True
    alert.save(update_fields=["notified", "updated_at"])


# --- infrastructure sync -------------------------------------------------------------

def sync_infrastructure(event) -> dict:
    """Pull RFP + handset state from the DECT system and mirror it into the database."""
    dect = get_dect(event)
    now = timezone.now()
    summary = {"event": event.slug, "rfps": 0, "handsets": 0, "alerts": 0, "ok": True}

    health = dect.health()
    if not health.get("ok"):
        open_alert(event, "omm.unreachable", Alert.Severity.CRITICAL,
                   _("DECT system unreachable: %(err)s") % {"err": health.get("error") or "unknown"})
        summary.update(ok=False, error=health.get("error"))
        return summary
    if resolve_alerts(event, "omm.unreachable"):
        back = open_alert(event, "omm.reachable", Alert.Severity.INFO, _("DECT system reachable again"))
        back.resolved_at = now
        back.save(update_fields=["resolved_at", "updated_at"])

    rfp_infos = dect.list_rfps()
    handsets = dect.list_handsets()

    clusters: dict[str, SyncCluster] = {}
    seen_ids, rfps_by_omm = set(), {}
    for info in rfp_infos:
        cluster = None
        if info.cluster:
            cluster = clusters.get(info.cluster)
            if cluster is None:
                cluster, _c = SyncCluster.objects.get_or_create(event=event, cluster_id=str(info.cluster))
                clusters[info.cluster] = cluster
        rfp, created = RFP.objects.get_or_create(event=event, omm_id=str(info.id), defaults={"name": info.name})
        was_connected, was_synced = rfp.connected, rfp.synced
        rfp.name = info.name or rfp.name
        rfp.mac_address = info.mac or rfp.mac_address
        rfp.ip_address = info.ip or None
        rfp.cluster = cluster
        rfp.sync_source = info.sync_source or ""
        if info.location and not rfp.location:
            rfp.location = info.location[:120]
        rfp.connected, rfp.synced, rfp.active_calls = info.connected, info.synced, info.active_calls
        rfp.extra = {**(rfp.extra or {}), **(info.extra or {})}
        if info.connected:
            rfp.last_seen_at = now
        if created or was_connected != rfp.connected or was_synced != rfp.synced:
            rfp.last_state_change = now
        rfp.save()
        seen_ids.add(rfp.pk)
        rfps_by_omm[str(info.id)] = rfp

        if rfp.is_active:
            if not rfp.connected:
                summary["alerts"] += 1
                open_alert(event, "rfp.down", Alert.Severity.CRITICAL,
                           _("RFP %(name)s (%(loc)s) is down") % {"name": rfp.name, "loc": rfp.location or info.ip},
                           rfp=rfp)
            elif was_connected is False and resolve_alerts(event, "rfp.down", rfp):
                up = open_alert(event, "rfp.up", Alert.Severity.INFO,
                                _("RFP %(name)s is back up") % {"name": rfp.name}, rfp=rfp)
                up.resolved_at = now
                up.save(update_fields=["resolved_at", "updated_at"])

    # RFPs that vanished from the OMM listing count as down
    for rfp in RFP.objects.filter(event=event, is_active=True, connected=True).exclude(pk__in=seen_ids):
        rfp.connected = rfp.synced = False
        rfp.last_state_change = now
        rfp.save(update_fields=["connected", "synced", "last_state_change", "updated_at"])
        open_alert(event, "rfp.down", Alert.Severity.CRITICAL,
                   _("RFP %(name)s disappeared from the OMM") % {"name": rfp.name}, rfp=rfp)

    # handsets
    handset_counts = defaultdict(int)
    devices = {d.ipei: d for d in Device.objects.filter(event=event, type="dect").exclude(ipei="")}
    adopted = 0
    for hs in handsets:
        rfp = rfps_by_omm.get(str(hs.rfp_id)) if hs.rfp_id else None
        if rfp is not None:
            handset_counts[rfp.pk] += 1
        device = devices.get(hs.ipei)
        if device is None:
            # unknown to PET: with the claim feature on, take it into the pool so its user can dial the code
            try:
                from apps.dect.claim import adopt_handset

                device = adopt_handset(event, hs)
            except Exception:  # noqa: BLE001 - never break the poll loop
                log.exception("adopting handset %s failed", hs.ipei)
                device = None
            if device is None:
                continue
            devices[device.ipei] = device
            adopted += 1
        fields = []
        if hs.ppn and device.omm_ppn != hs.ppn:
            device.omm_ppn = hs.ppn
            fields.append("omm_ppn")
        if hs.user_id and device.omm_user_id != hs.user_id:
            device.omm_user_id = hs.user_id
            fields.append("omm_user_id")
        if rfp is not None:
            device.last_seen_rfp, device.last_seen_at = rfp, now
            fields += ["last_seen_rfp", "last_seen_at"]
        if hs.battery is not None:
            device.battery_percent = max(0, min(100, hs.battery))
            fields.append("battery_percent")
        if hs.rssi is not None:
            device.rssi = hs.rssi
            fields.append("rssi")
        if hs.model and not device.handset_model:
            device.handset_model = hs.model[:60]
            fields.append("handset_model")
        if hs.subscribed and device.state in (Device.State.NEW, Device.State.PENDING, Device.State.OFFLINE):
            device.state = Device.State.SUBSCRIBED
            fields.append("state")
        elif not hs.subscribed and device.state == Device.State.SUBSCRIBED:
            device.state = Device.State.PENDING
            fields.append("state")
        if fields:
            device.save(update_fields=list(set(fields)) + ["updated_at"])

    # samples + cluster health
    RFPStatusSample.objects.bulk_create([
        RFPStatusSample(rfp=rfp, at=now, connected=rfp.connected, synced=rfp.synced,
                        active_calls=rfp.active_calls, handsets=handset_counts.get(rfp.pk, 0))
        for rfp in rfps_by_omm.values()
    ])
    unsynced = list(RFP.objects.filter(event=event, is_active=True, connected=True, synced=False)
                    .select_related("cluster").order_by("cluster__cluster_id", "name"))
    if unsynced:
        summary["alerts"] += 1
        open_alert(event, "sync.degraded", Alert.Severity.WARNING,
                   _("Sync degraded: %(rfps)s not in sync")
                   % {"rfps": ", ".join(f"{r.name} ({r.cluster or '-'})" for r in unsynced)})
    else:
        resolve_alerts(event, "sync.degraded")

    summary.update(rfps=len(rfp_infos), handsets=len(handsets), clusters=len(clusters), adopted=adopted)
    return summary


def sync_handset_positions(event) -> int:
    """Lightweight variant of ``sync_infrastructure``: only refresh which RFP serves each handset."""
    dect = get_dect(event)
    rfps = {r.omm_id: r for r in RFP.objects.filter(event=event)}
    devices = {d.ipei: d for d in Device.objects.filter(event=event, type="dect").exclude(ipei="")}
    now, n = timezone.now(), 0
    for hs in dect.list_handsets():
        device, rfp = devices.get(hs.ipei), rfps.get(str(hs.rfp_id))
        if device is None or rfp is None:
            continue
        device.last_seen_rfp, device.last_seen_at = rfp, now
        if hs.rssi is not None:
            device.rssi = hs.rssi
        device.save(update_fields=["last_seen_rfp", "last_seen_at", "rssi", "updated_at"])
        n += 1
    return n


# --- site survey -------------------------------------------------------------------

def _device_for_number(event, number: str):
    return (Device.objects.filter(event=event, type="dect", bindings__is_active=True,
                                  bindings__extension__event=event, bindings__extension__number=number)
            .select_related("last_seen_rfp").order_by("bindings__priority").first())


def log_site_survey(event, caller_number: str) -> str:
    """Called by the PBX when the survey number is dialled; returns the RFP name to announce."""
    device = _device_for_number(event, caller_number)
    rfp, rssi = None, None
    if device is not None:
        # prefer a live lookup so walk-tests reflect the current cell, fall back to last poll
        try:
            rfps = {r.omm_id: r for r in RFP.objects.filter(event=event)}
            for hs in get_dect(event).list_handsets():
                if hs.ipei == device.ipei:
                    rfp, rssi = rfps.get(str(hs.rfp_id)), hs.rssi
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("live handset lookup failed during site survey: %s", exc)
        if rfp is None:
            rfp, rssi = device.last_seen_rfp, device.rssi
        if rfp is not None:
            device.last_seen_rfp, device.last_seen_at = rfp, timezone.now()
            device.save(update_fields=["last_seen_rfp", "last_seen_at", "updated_at"])
    SiteSurveyLog.objects.create(event=event, device=device, rfp=rfp, rssi=rssi,
                                 note="" if device else f"unknown caller {caller_number}"[:200])
    return rfp.name if rfp is not None else "unknown"


# --- coverage --------------------------------------------------------------------

def coverage_summary(event) -> dict:
    rfps = list(RFP.objects.filter(event=event).select_related("cluster").annotate(n_handsets=Count("handsets")))
    clusters = []
    for c in SyncCluster.objects.filter(event=event).prefetch_related("rfps"):
        members = [r for r in rfps if r.cluster_id == c.pk]
        clusters.append({
            "id": c.pk, "cluster_id": c.cluster_id, "name": str(c), "health": c.health,
            "rfps": len(members), "up": sum(1 for r in members if r.connected),
            "handsets": sum(r.n_handsets for r in members),
        })
    return {
        "event": event.slug,
        "rfps": [{"id": r.pk, "omm_id": r.omm_id, "name": r.name, "status": r.status, "cluster": r.cluster.cluster_id
                  if r.cluster else None, "handsets": r.n_handsets, "active_calls": r.active_calls,
                  "location": r.location, "pos_x": r.pos_x, "pos_y": r.pos_y} for r in rfps],
        "clusters": clusters,
        "totals": {"rfps": len(rfps), "up": sum(1 for r in rfps if r.status == "up"),
                   "down": sum(1 for r in rfps if r.status == "down"),
                   "unsynced": sum(1 for r in rfps if r.status == "unsynced"),
                   "handsets": sum(r.n_handsets for r in rfps),
                   "open_alerts": Alert.objects.filter(event=event, resolved_at__isnull=True).count()},
        "weak_zones": weak_zones(event),
    }


def weak_zones(event) -> list[dict]:
    """Heuristic: RFPs that are down, or empty while their cluster neighbours carry handsets."""
    rfps = list(RFP.objects.filter(event=event, is_active=True).annotate(n_handsets=Count("handsets")))
    by_cluster = defaultdict(list)
    for r in rfps:
        by_cluster[r.cluster_id].append(r)
    out = []
    for r in rfps:
        if not r.connected:
            out.append({"rfp": r.name, "id": r.pk, "reason": "down", "location": r.location})
            continue
        neighbours = [n for n in by_cluster[r.cluster_id] if n.pk != r.pk and n.connected]
        if neighbours and r.n_handsets == 0 and sum(n.n_handsets for n in neighbours) >= 2 * len(neighbours):
            out.append({"rfp": r.name, "id": r.pk, "reason": "no_handsets", "location": r.location})
        elif not r.synced:
            out.append({"rfp": r.name, "id": r.pk, "reason": "unsynced", "location": r.location})
    return out
