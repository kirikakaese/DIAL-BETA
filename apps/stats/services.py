"""Statistics services: CDR ingest (hook + Asterisk table sweep), hourly/extension aggregation,
retention, dashboard data and GDPR export/delete."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, F, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from apps.core.features import enabled
from apps.dect.models import RFP, RFPStatusSample, SiteSurveyLog
from apps.devices.models import Device
from apps.events.models import Event
from apps.extensions.models import Extension, ExtensionType
from apps.pbx import dialplan as dp
from apps.pbx.models import Cdr

from .models import CallRecord, ExtensionStat, HourlyStat

log = logging.getLogger("pet.stats")

ANSWERED = "ANSWERED"
PRIVATE_LABEL = "private"
_CHANNEL_RE = re.compile(r"^(?:PJSIP|SIP)/(?P<user>[^-/@]+)")


# --------------------------------------------------------------------------- parsing helpers

def _parse_dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        d = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value)):
        d = dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
    else:
        d = parse_datetime(str(value))
        if d is None:
            try:
                d = dt.datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if timezone.is_naive(d):
        d = timezone.make_aware(d, dt.UTC)
    return d


def _int(value) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _hour(d: dt.datetime) -> dt.datetime:
    return d.replace(minute=0, second=0, microsecond=0)


def _hash(number: str) -> str:
    return hashlib.sha1(f"{settings.SECRET_KEY}:{number}".encode()).hexdigest()[:12]


def _extension(event, number: str) -> Extension | None:
    if not number:
        return None
    qs = Extension.objects.filter(event=event, number=number).exclude(state=Extension.State.DELETED)
    return qs.order_by("-created_at").select_related("owner").first()


def _rfp(event, ref, src_ext: Extension | None) -> RFP | None:
    if ref not in (None, ""):
        ref = str(ref)
        rfp = RFP.objects.filter(event=event).filter(
            Q(omm_id=ref) | Q(name=ref) | Q(pk=ref if ref.isdigit() else None)).first()
        if rfp is not None:
            return rfp
    if src_ext is not None and src_ext.type == ExtensionType.DECT:
        dev = (Device.objects.filter(bindings__extension=src_ext, bindings__is_active=True, last_seen_rfp__isnull=False)
               .select_related("last_seen_rfp").first())
        if dev is not None:
            return dev.last_seen_rfp
    return None


def _answered_number(event, dstchannel: str) -> str | None:
    """``PJSIP/<sip_username>-00000042`` -> number of the device's primary extension (best effort)."""
    m = _CHANNEL_RE.match(dstchannel or "")
    if not m:
        return None
    user = m.group("user")
    dev = Device.objects.filter(event=event, sip_username=user).first()
    if dev is not None:
        ext = dev.primary_extension
        return ext.number if ext else None
    ext = Extension.objects.filter(event=event, number=user).active().first()
    return ext.number if ext else None


def _synthetic_uniqueid(event, src, dst, started_at) -> str:
    return "pet-" + hashlib.sha1(f"{event.pk}:{src}:{dst}:{started_at.isoformat()}".encode()).hexdigest()[:32]


def retention_days(event) -> int:
    days = event.cdr_retention_days
    if days is None:
        days = getattr(settings, "PET_CDR_RETENTION_DAYS", 30)
    return int(days or 0)


# --------------------------------------------------------------------------- ingest

def _normalize(event, record: dict) -> dict:
    src = str(record.get("src") or "").strip()
    dst = str(record.get("dst") or "").strip()
    start = _parse_dt(record.get("start")) or timezone.now()
    answer = _parse_dt(record.get("answer"))
    end = _parse_dt(record.get("end"))
    duration = _int(record.get("duration"))
    billsec = _int(record.get("billsec"))
    if end is None and duration:
        end = start + dt.timedelta(seconds=duration)
    disposition = str(record.get("disposition") or "").strip().upper() or ("ANSWERED" if billsec else "NO ANSWER")
    src_ext = _extension(event, src)
    dst_ext = _extension(event, dst)
    uniqueid = str(record.get("uniqueid") or "").strip() or _synthetic_uniqueid(event, src, dst, start)
    return {
        "event": event, "started_at": start, "answered_at": answer, "ended_at": end, "src_number": src,
        "dst_number": dst, "src_extension": src_ext, "dst_extension": dst_ext, "duration": duration,
        "billsec": billsec, "disposition": disposition[:20],
        "src_type": src_ext.type if src_ext else "", "dst_type": dst_ext.type if dst_ext else "",
        "rfp": _rfp(event, record.get("rfp"), src_ext), "uniqueid": uniqueid[:150],
    }


def ingest_cdr(event, record: dict) -> CallRecord | None:
    """PBX ``cdr`` hook entry point. Idempotent on ``uniqueid``.

    In ``cdr_aggregate_only`` mode nothing per-call is stored - only the hourly / per-extension
    counters are bumped and an *unsaved* :class:`CallRecord` is returned (``pk is None``).
    """
    if not enabled("stats", event):
        return None
    if not isinstance(record, dict) or not (record.get("src") or record.get("dst")):
        return None
    data = _normalize(event, record)
    if event.cdr_aggregate_only:
        if not cache.add(f"stats:cdr:{event.pk}:{data['uniqueid']}", 1, timeout=7 * 86400):
            return CallRecord(**data)  # duplicate notify
        rec = CallRecord(**data)
        _bump_aggregates(rec)
        _notify_callgroups(event, rec, record)
        return rec
    existing = CallRecord.objects.filter(uniqueid=data["uniqueid"]).first()
    if existing is not None:
        return existing
    raw = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in record.items()}
    try:
        with transaction.atomic():
            rec = CallRecord.objects.create(raw=raw, **data)
    except IntegrityError:
        return CallRecord.objects.filter(uniqueid=data["uniqueid"]).first()
    _bump_aggregates(rec)
    _notify_callgroups(event, rec, record)
    return rec


def _notify_callgroups(event, rec: CallRecord, record: dict) -> None:
    if rec.dst_type != ExtensionType.GROUP or rec.disposition != ANSWERED:
        return
    try:
        from apps.callgroups import services as cg

        cg.record_call_from_cdr(event, rec.dst_number, _answered_number(event, str(record.get("dstchannel") or "")),
                                at=rec.answered_at or rec.started_at)
    except Exception:  # noqa: BLE001 - statistics must never break because of an optional package
        log.debug("callgroups notification skipped", exc_info=True)


def _bump_aggregates(rec: CallRecord) -> None:
    """Incrementally update HourlyStat / ExtensionStat for one call."""
    hour = _hour(rec.started_at)
    with transaction.atomic():
        hs, _c = HourlyStat.objects.select_for_update().get_or_create(event=rec.event, hour=hour)
        hs.calls += 1
        if rec.disposition == ANSWERED:
            hs.answered += 1
        hs.total_billsec += rec.billsec
        h = _hash(rec.src_number)
        if rec.src_number and h not in hs.caller_hashes:
            hs.caller_hashes = list(hs.caller_hashes) + [h]
        hs.unique_callers = len(hs.caller_hashes)
        key = rec.dst_type or "external"
        hs.by_type = {**hs.by_type, key: hs.by_type.get(key, 0) + 1}
        hs.by_disposition = {**hs.by_disposition, rec.disposition: hs.by_disposition.get(rec.disposition, 0) + 1}
        if rec.rfp_id:
            entry = dict(hs.by_rfp.get(str(rec.rfp_id), {}))
            entry["calls"] = entry.get("calls", 0) + 1
            entry["billsec"] = entry.get("billsec", 0) + rec.billsec
            entry["erlang"] = round(entry["billsec"] / 3600, 4)
            hs.by_rfp = {**hs.by_rfp, str(rec.rfp_id): entry}
        hs.save()
    day = timezone.localtime(rec.started_at).date()
    if rec.src_extension_id:
        st, _c = ExtensionStat.objects.get_or_create(event=rec.event, extension_id=rec.src_extension_id, date=day)
        ExtensionStat.objects.filter(pk=st.pk).update(
            outbound=F("outbound") + 1, answered=F("answered") + (1 if rec.disposition == ANSWERED else 0),
            total_billsec=F("total_billsec") + rec.billsec)
    if rec.dst_extension_id:
        st, _c = ExtensionStat.objects.get_or_create(event=rec.event, extension_id=rec.dst_extension_id, date=day)
        ExtensionStat.objects.filter(pk=st.pk).update(
            inbound=F("inbound") + 1, answered=F("answered") + (1 if rec.disposition == ANSWERED else 0),
            total_billsec=F("total_billsec") + rec.billsec)


def ingest_from_asterisk_table(event, limit: int = 5000) -> int:
    """Fallback to the hook: sweep unprocessed rows Asterisk wrote into the realtime ``cdr`` table."""
    ctx = dp.event_context(event)
    qs = (Cdr.objects.filter(ingested_at__isnull=True)
          .filter(Q(dcontext=ctx) | Q(dcontext__startswith=ctx + "-") | Q(accountcode=event.slug))
          .order_by("start", "id")[:limit])
    n = 0
    now = timezone.now()
    for row in qs:
        rec = row.as_record()
        rec["accountcode"] = row.accountcode
        if row.userfield and row.userfield.startswith("rfp="):
            rec["rfp"] = row.userfield[4:].split(";")[0]
        res = ingest_cdr(event, rec)
        Cdr.objects.filter(pk=row.pk).update(ingested_at=now)
        if res is not None:
            n += 1
    return n


# --------------------------------------------------------------------------- aggregation

def aggregate_hourly(event, since=None) -> int:
    """(Re)build HourlyStat rows from CallRecords (exact) and RFPStatusSample averages (DECT load).

    Hours without CallRecords keep their incrementally bumped counters (aggregate-only mode).
    Returns the number of HourlyStat rows written.
    """
    since = since or (timezone.now() - dt.timedelta(hours=48))
    since = _hour(since)
    hours: dict[dt.datetime, dict] = {}
    for rec in CallRecord.objects.filter(event=event, started_at__gte=since).select_related(None).iterator():
        h = _hour(rec.started_at)
        agg = hours.setdefault(h, {"calls": 0, "answered": 0, "billsec": 0, "callers": set(), "by_type": {},
                                   "by_disposition": {}, "by_rfp": {}})
        agg["calls"] += 1
        agg["answered"] += rec.disposition == ANSWERED
        agg["billsec"] += rec.billsec
        if rec.src_number:
            agg["callers"].add(_hash(rec.src_number))
        key = rec.dst_type or "external"
        agg["by_type"][key] = agg["by_type"].get(key, 0) + 1
        agg["by_disposition"][rec.disposition] = agg["by_disposition"].get(rec.disposition, 0) + 1
        if rec.rfp_id:
            e = agg["by_rfp"].setdefault(str(rec.rfp_id), {"calls": 0, "billsec": 0})
            e["calls"] += 1
            e["billsec"] += rec.billsec
    # DECT load from status samples: average concurrent calls per RFP per hour == Erlang
    samples = (RFPStatusSample.objects.filter(rfp__event=event, at__gte=since)
               .values("rfp_id", "at", "active_calls"))
    dect_load: dict[dt.datetime, dict[str, list[int]]] = {}
    for s in samples:
        dect_load.setdefault(_hour(s["at"]), {}).setdefault(str(s["rfp_id"]), []).append(s["active_calls"])
    written = 0
    for h in sorted(set(hours) | set(dect_load)):
        agg = hours.get(h)
        hs, _c = HourlyStat.objects.get_or_create(event=event, hour=h)
        if agg is not None:
            hs.calls, hs.answered, hs.total_billsec = agg["calls"], agg["answered"], agg["billsec"]
            hs.caller_hashes = sorted(agg["callers"])
            hs.unique_callers = len(agg["callers"])
            hs.by_type, hs.by_disposition = agg["by_type"], agg["by_disposition"]
            by_rfp = {k: {**v, "erlang": round(v["billsec"] / 3600, 4)} for k, v in agg["by_rfp"].items()}
        else:
            by_rfp = {k: dict(v) for k, v in hs.by_rfp.items()}
        for rfp_id, vals in dect_load.get(h, {}).items():
            entry = by_rfp.setdefault(rfp_id, {"calls": 0, "billsec": 0, "erlang": 0.0})
            entry["erlang_dect"] = round(sum(vals) / len(vals), 4)
            entry["samples"] = len(vals)
        hs.by_rfp = by_rfp
        hs.save()
        written += 1
    return written


def enforce_retention(event) -> int:
    """Delete CallRecords older than the event's retention (``0`` = keep forever). Returns #deleted."""
    days = retention_days(event)
    if days <= 0:
        return 0
    cutoff = timezone.now() - dt.timedelta(days=days)
    n, _detail = CallRecord.objects.filter(event=event, started_at__lt=cutoff).delete()
    if n:
        log.info("stats: purged %d call records of %s older than %d days", n, event.slug, days)
    return n


# --------------------------------------------------------------------------- dashboard

def _label(ext: Extension | None, number: str) -> str:
    if ext is None:
        return number or "?"
    if not ext.in_phonebook:
        return PRIVATE_LABEL
    return f"{ext.number} {ext.display_name}".strip() if ext.display_name else ext.number


def dashboard_data(event, hours: int = 48) -> dict:
    now = timezone.now()
    end = _hour(now)
    start = end - dt.timedelta(hours=hours - 1)
    rows = {hs.hour: hs for hs in HourlyStat.objects.filter(event=event, hour__gte=start, hour__lte=end)}
    series = []
    totals = {"calls": 0, "answered": 0, "billsec": 0}
    dispositions: dict[str, int] = {}
    by_type: dict[str, int] = {}
    rfp_acc: dict[str, dict] = {}
    callers: set[str] = set()
    h = start
    while h <= end:
        hs = rows.get(h)
        series.append({"hour": h.isoformat(), "label": timezone.localtime(h).strftime("%a %H:%M"),
                       "calls": hs.calls if hs else 0, "answered": hs.answered if hs else 0,
                       "billsec": hs.total_billsec if hs else 0})
        if hs:
            totals["calls"] += hs.calls
            totals["answered"] += hs.answered
            totals["billsec"] += hs.total_billsec
            callers.update(hs.caller_hashes or [])
            for k, v in (hs.by_disposition or {}).items():
                dispositions[k] = dispositions.get(k, 0) + v
            for k, v in (hs.by_type or {}).items():
                by_type[k] = by_type.get(k, 0) + v
            for rid, e in (hs.by_rfp or {}).items():
                acc = rfp_acc.setdefault(rid, {"calls": 0, "billsec": 0, "erlang_sum": 0.0, "erlang_max": 0.0,
                                               "dect_sum": 0.0, "dect_hours": 0, "hours": 0})
                acc["calls"] += e.get("calls", 0)
                acc["billsec"] += e.get("billsec", 0)
                acc["erlang_sum"] += e.get("erlang", 0.0)
                acc["erlang_max"] = max(acc["erlang_max"], e.get("erlang", 0.0), e.get("erlang_dect", 0.0))
                acc["hours"] += 1
                if "erlang_dect" in e:
                    acc["dect_sum"] += e["erlang_dect"]
                    acc["dect_hours"] += 1
        h += dt.timedelta(hours=1)

    # busiest extensions (respecting phonebook privacy)
    day_from = timezone.localtime(start).date()
    ext_rows = (ExtensionStat.objects.filter(event=event, date__gte=day_from)
                .values("extension_id").annotate(inbound=Sum("inbound"), outbound=Sum("outbound"),
                                                 answered=Sum("answered"), billsec=Sum("total_billsec"))
                .annotate(total=F("inbound") + F("outbound")).order_by("-total")[:10])
    exts = {e.pk: e for e in Extension.objects.filter(pk__in=[r["extension_id"] for r in ext_rows])}
    top = []
    for r in ext_rows:
        ext = exts.get(r["extension_id"])
        top.append({"label": _label(ext, ""), "type": ext.type if ext else "", "inbound": r["inbound"],
                    "outbound": r["outbound"], "total": r["total"], "answered": r["answered"], "billsec": r["billsec"],
                    "private": bool(ext and not ext.in_phonebook)})

    rfps = {str(r.pk): r for r in RFP.objects.filter(event=event)}
    rfp_table = []
    for rid, r in rfps.items():
        acc = rfp_acc.get(rid, {})
        hrs = acc.get("hours", 0) or 1
        rfp_table.append({
            "id": r.pk, "name": r.name, "location": r.location, "status": r.status, "calls": acc.get("calls", 0),
            "erlang_avg": round(acc.get("erlang_sum", 0.0) / hrs, 3),
            "erlang_dect_avg": round(acc["dect_sum"] / acc["dect_hours"], 3) if acc.get("dect_hours") else None,
            "erlang_max": round(acc.get("erlang_max", 0.0), 3),
            "handsets": Device.objects.filter(last_seen_rfp=r).count(),
            "surveys": SiteSurveyLog.objects.filter(rfp=r, at__gte=start).count(),
        })
    max_erlang = max([r["erlang_max"] for r in rfp_table] + [0.0])
    for r in rfp_table:
        r["load_pct"] = round(100 * r["erlang_max"] / max_erlang) if max_erlang else 0
    heat_max = max([r["handsets"] + r["surveys"] + r["calls"] for r in rfp_table] + [1])
    heatmap = [{"name": r["name"], "handsets": r["handsets"], "surveys": r["surveys"], "calls": r["calls"],
                "intensity": round((r["handsets"] + r["surveys"] + r["calls"]) / heat_max, 3)} for r in rfp_table]

    answered = totals["answered"]
    return {
        "event": event.slug, "hours": hours, "from": start.isoformat(), "to": end.isoformat(),
        "aggregate_only": event.cdr_aggregate_only, "retention_days": retention_days(event),
        "series": series, "totals": {**totals, "unique_callers": len(callers),
                                     "avg_duration": round(totals["billsec"] / answered) if answered else 0,
                                     "answer_rate": round(100 * answered / totals["calls"]) if totals["calls"] else 0},
        "dispositions": dict(sorted(dispositions.items(), key=lambda kv: -kv[1])),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "top_extensions": top, "rfps": sorted(rfp_table, key=lambda r: -r["erlang_max"]), "heatmap": heatmap,
        "records_kept": CallRecord.objects.filter(event=event).count() if not event.cdr_aggregate_only else 0,
    }


def export_csv(event) -> bytes:
    """CSV of CallRecords (or of HourlyStat rows in aggregate-only mode)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    if event.cdr_aggregate_only:
        w.writerow(["hour", "calls", "answered", "total_billsec", "unique_callers", "by_type", "by_disposition"])
        for hs in HourlyStat.objects.filter(event=event):
            w.writerow([hs.hour.isoformat(), hs.calls, hs.answered, hs.total_billsec, hs.unique_callers,
                        hs.by_type, hs.by_disposition])
    else:
        w.writerow(["started_at", "answered_at", "ended_at", "src", "dst", "src_type", "dst_type", "duration",
                    "billsec", "disposition", "rfp", "uniqueid"])
        for r in CallRecord.objects.filter(event=event).select_related("rfp").iterator():
            w.writerow([r.started_at.isoformat(), r.answered_at.isoformat() if r.answered_at else "",
                        r.ended_at.isoformat() if r.ended_at else "", r.src_number, r.dst_number, r.src_type,
                        r.dst_type, r.duration, r.billsec, r.disposition, r.rfp.name if r.rfp else "", r.uniqueid])
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- per-user / GDPR

def _user_records(user):
    return (CallRecord.objects.filter(Q(src_extension__owner=user) | Q(dst_extension__owner=user))
            .select_related("event", "src_extension", "dst_extension").distinct())


def my_calls(user, event=None, limit: int = 200):
    qs = _user_records(user)
    if event is not None:
        if event.cdr_aggregate_only:
            return CallRecord.objects.none()
        qs = qs.filter(event=event)
    return qs.order_by("-started_at")[:limit]


def gdpr_export(user) -> dict:
    """Everything statistics-related PET stores about ``user``: their extensions and call records."""
    exts = Extension.objects.filter(owner=user).select_related("event").order_by("event__start_date", "number")
    return {
        "user": {"username": user.username, "email": user.email},
        "generated_at": timezone.now().isoformat(),
        "extensions": [{"event": e.event.slug, "number": e.number, "type": e.type, "state": e.state,
                        "display_name": e.display_name, "in_phonebook": e.in_phonebook} for e in exts],
        "calls": [{
            "event": r.event.slug, "started_at": r.started_at.isoformat(),
            "answered_at": r.answered_at.isoformat() if r.answered_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "src": r.src_number, "dst": r.dst_number,
            "direction": "outbound" if (r.src_extension and r.src_extension.owner_id == user.pk) else "inbound",
            "duration": r.duration, "billsec": r.billsec, "disposition": r.disposition,
        } for r in _user_records(user).order_by("started_at")],
    }


def gdpr_delete(user) -> int:
    """Anonymize ``user`` in all CallRecords (numbers -> ``anon``, extension links removed). Returns #rows."""
    n = 0
    for rec in _user_records(user):
        if rec.src_extension_id and rec.src_extension.owner_id == user.pk:
            rec.src_number, rec.src_extension = "anon", None
        if rec.dst_extension_id and rec.dst_extension.owner_id == user.pk:
            rec.dst_number, rec.dst_extension = "anon", None
        rec.raw = {}
        rec.save(update_fields=["src_number", "dst_number", "src_extension", "dst_extension", "raw"])
        n += 1
    ExtensionStat.objects.filter(extension__owner=user).delete()
    from apps.core.audit import log as audit

    audit(action="delete", actor=user, target=user, message=f"GDPR: anonymized {n} call records")
    return n


def live_events():
    return Event.objects.live()


__all__ = [
    "ingest_cdr", "ingest_from_asterisk_table", "aggregate_hourly", "enforce_retention", "dashboard_data",
    "export_csv", "my_calls", "gdpr_export", "gdpr_delete", "retention_days", "live_events", "_",
    "Avg", "Count",
]
