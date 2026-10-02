"""Asterisk backend: realtime tables (provisioning) + ARI (call control) + optional AMI.

Provisioning never talks to Asterisk - it writes the ``ps_*`` / ``extensions`` /
``voicemail_users`` tables that Asterisk reads live via res_config_odbc, so it
works even while the PBX is down. Call control and status use ARI.

In *agent* provisioning mode (``PBXConnection.provisioning == "agent"``) the same rows are the
source of the snapshots a venue agent pulls; ARI/AMI are optional then (the venue box is usually
not reachable from PET), reloads are left to the agent and registration status comes from the
``ps_contacts`` rows the agent reports with its heartbeats.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from django.conf import settings
from django.db import transaction

from apps.pbx import dialplan as dp
from apps.pbx.ari import ARIClient, ARIError
from apps.pbx.base import ChannelState, ExtensionStatus, PBXAdapter, PBXError
from apps.pbx.models import (
    DialplanEntry,
    PBXSyncLog,
    PsAor,
    PsAuth,
    PsContact,
    PsEndpoint,
    VoicemailUser,
)

log = logging.getLogger("pet.pbx.asterisk")

# Device types that register with Asterisk over SIP (DECT handsets do so via the OMM).
SIP_DEVICE_TYPES = {"dect", "sip", "webrtc", "analog"}

_DEVICE_STATE_MAP = {
    "NOT_INUSE": "idle", "INUSE": "inuse", "BUSY": "busy", "RINGING": "ringing", "RINGINUSE": "ringing",
    "ONHOLD": "inuse", "UNAVAILABLE": "unavailable", "INVALID": "unavailable", "UNKNOWN": "unavailable",
}
_STATE_RANK = {"ringing": 4, "inuse": 3, "busy": 2, "idle": 1, "unavailable": 0}
_CHANNEL_STATE_MAP = {
    "Up": "up", "Ring": "ringing", "Ringing": "ringing", "Busy": "busy", "Down": "down",
    "Rsrvd": "down", "OffHook": "up", "Dialing": "ringing", "Dialing Offhook": "ringing",
    "Pre-ring": "ringing", "Unknown": "down",
}


def _yesno(v: bool) -> str:
    return "yes" if v else "no"


class AsteriskPBX(PBXAdapter):
    name = "asterisk"

    def __init__(self, ari: ARIClient | None = None, config: dict | None = None):
        cfg = dict(settings.ASTERISK)
        cfg.update(config or {})
        self.cfg = cfg
        self.ari = ari or ARIClient(cfg["ARI_URL"], cfg["ARI_USER"], cfg["ARI_PASSWORD"],
                                    app=cfg.get("ARI_APP", "pet"),
                                    timeout=getattr(settings, "PET_PBX_HTTP_TIMEOUT", 5))
        self.codecs = getattr(settings, "PET_PBX_CODECS", "alaw,ulaw,g722")
        self.webrtc_codecs = getattr(settings, "PET_PBX_WEBRTC_CODECS", "opus,alaw,ulaw")
        self.use_ami = bool(getattr(settings, "PET_PBX_USE_AMI", True) and cfg.get("AMI_HOST"))
        self.agent_mode = cfg.get("PROVISIONING") == "agent"
        self.has_ari = bool(cfg.get("ARI_URL"))

    @property
    def _ari_offline(self) -> bool:
        """Agent mode without an ARI URL: never try to reach the venue box from PET."""
        return self.agent_mode and not self.has_ari

    def _agent_connection(self):
        from apps.pbx.models import PBXConnection

        pk = self.cfg.get("EVENT_ID")
        return PBXConnection.objects.filter(event_id=pk).select_related("event").first() if pk else None

    # ------------------------------------------------------------------ helpers
    def _ami(self):
        from apps.pbx.ami import AMIClient

        return AMIClient(self.cfg["AMI_HOST"], int(self.cfg.get("AMI_PORT") or 5038),
                         self.cfg.get("AMI_USER", ""), self.cfg.get("AMI_PASSWORD", ""))

    @staticmethod
    def _log(event, kind, target, ok=True, message="", rows=0):
        try:
            PBXSyncLog.objects.create(event=event, kind=kind, target=str(target)[:120], ok=ok,
                                      message=str(message)[:4000], rows=rows)
        except Exception:  # noqa: BLE001 - logging must never break provisioning
            log.debug("could not write PBXSyncLog", exc_info=True)

    @staticmethod
    def _callerid(device, ext) -> str:
        """``"<name>" <number>`` - the name part follows ``ext.display_mode`` (GURU3 semantics)."""
        if ext is not None:
            name = ext.caller_id_display
            number = ext.number
        else:
            name = device.name or device.sip_username
            number = device.sip_username
        name = name.replace('"', "").replace("<", "").replace(">", "")
        # ps_endpoints.callerid is VARCHAR(40)
        room = 40 - len(number) - 5
        return f'"{name[:max(room, 0)]}" <{number}>'

    @staticmethod
    def _primary_extension(device):
        """Like ``device.primary_extension`` but only considers *active* extensions."""
        b = (device.bindings.filter(is_active=True, extension__state="active")
             .select_related("extension", "extension__owner", "extension__event")
             .order_by("priority").first())
        return b.extension if b else None

    def _mailbox_for(self, device, ext) -> str | None:
        if ext is None or not dp.voicemail_enabled(device.event):
            return None
        return dp.mailbox(ext)

    # ------------------------------------------------------------------ devices
    @transaction.atomic
    def sync_device(self, device) -> None:
        if device.type not in SIP_DEVICE_TYPES:
            log.debug("device %s (%s) is not a SIP endpoint - skipping", device.pk, device.type)
            return
        if not device.sip_username or not device.sip_password:
            device.ensure_sip_credentials(save=True)
        ext = self._primary_extension(device)
        # (for a trunk's SIP account ``ext`` is the trunk: callerid becomes the block base, e.g. "Village" <4700>)
        event = device.event
        ctx = dp.event_context(event)
        user = device.sip_username
        transport = f"transport-{device.sip_transport or 'udp'}"
        is_webrtc = device.type == "webrtc" or device.sip_transport == "wss"
        is_dect = device.type == "dect"
        mailbox = self._mailbox_for(device, ext)
        # call waiting off -> the endpoint counts as BUSY with one call, so a second caller gets BUSY
        # (DECT handsets cannot knock anyway and always get busy_at=1)
        no_call_waiting = ext is not None and not ext.call_waiting

        PsAuth.objects.update_or_create(id=user, defaults={
            "auth_type": "userpass", "username": user, "password": device.sip_password,
        })
        PsAor.objects.update_or_create(id=user, defaults={
            "max_contacts": 1 if is_dect else getattr(settings, "PET_PBX_MAX_CONTACTS", 3),
            "remove_existing": "yes", "remove_unavailable": "yes",
            "qualify_frequency": 0 if is_dect else 60,
            "default_expiration": 300, "minimum_expiration": 60, "maximum_expiration": 3600,
            "mailboxes": mailbox, "voicemail_extension": None,
        })
        PsEndpoint.objects.update_or_create(id=user, defaults={
            "transport": transport if not is_webrtc else "transport-wss",
            "aors": user, "auth": user, "context": ctx,
            "disallow": "all", "allow": self.webrtc_codecs if is_webrtc else self.codecs,
            "direct_media": "no", "rtp_symmetric": "yes", "force_rport": "yes", "rewrite_contact": "yes",
            "dtmf_mode": "rfc4733", "callerid": self._callerid(device, ext), "mailboxes": mailbox,
            "language": (ext.language if ext and ext.language else event.default_language) or None,
            "set_var": f"PET_EVENT={event.slug}", "accountcode": event.slug[:20],
            "identify_by": "username", "send_pai": "yes", "trust_id_inbound": "no",
            "allow_subscribe": "yes", "subscribe_context": ctx,
            "device_state_busy_at": 1 if (is_dect or no_call_waiting) else None,
            "ice_support": _yesno(is_webrtc), "webrtc": _yesno(is_webrtc),
            "media_encryption": "dtls" if is_webrtc else ("sdes" if device.sip_transport == "tls" else "no"),
            "use_avpf": _yesno(is_webrtc), "dtls_auto_generate_cert": _yesno(is_webrtc),
            "media_use_received_transport": _yesno(is_webrtc), "moh_suggest": "default",
        })
        self._log(event, PBXSyncLog.Kind.DEVICE, user, rows=3)

    @transaction.atomic
    def remove_device(self, device) -> None:
        user = device.sip_username
        if not user:
            return
        n = PsEndpoint.objects.filter(id=user).delete()[0]
        n += PsAuth.objects.filter(id=user).delete()[0]
        n += PsAor.objects.filter(id=user).delete()[0]
        self._log(device.event, PBXSyncLog.Kind.REMOVE, f"device {user}", rows=n)

    # ------------------------------------------------------------------ extensions
    def _write_rows(self, ctx: str, rows: list[dp.Row]) -> int:
        extens = {r.exten for r in rows}
        DialplanEntry.objects.filter(context=ctx, exten__in=extens).delete()
        DialplanEntry.objects.bulk_create([
            DialplanEntry(context=ctx, exten=r.exten, priority=r.priority, app=r.app, appdata=r.appdata)
            for r in rows
        ])
        return len(rows)

    def _sync_voicemail_user(self, ext) -> None:
        ctx = dp.event_context(ext.event)
        wants_box = dp.voicemail_enabled(ext.event) and (ext.is_endpoint or ext.type == "voicemail")
        if not wants_box:
            VoicemailUser.objects.filter(context=ctx, mailbox=ext.number).delete()
            return
        cfg = ext.config or {}
        VoicemailUser.objects.update_or_create(context=ctx, mailbox=ext.number, defaults={
            "password": str(cfg.get("voicemail_pin") or ext.number)[:80],
            "fullname": ext.caller_id_name[:80],
            "email": (ext.owner.email if ext.owner and cfg.get("voicemail_email") else "")[:80],
            "attach": "no", "language": (ext.language or ext.event.default_language or None),
            "tz": None, "saycid": "yes", "envelope": "yes", "review": "no", "maxmsg": 50,
            "stamp": datetime.now(UTC),
        })

    @transaction.atomic
    def sync_extension(self, ext) -> None:
        for b in ext.bindings.filter(is_active=True).select_related("device"):
            self.sync_device(b.device)
        ctx = dp.event_context(ext.event)
        rows = dp.rows_for_extension(ext)
        n = self._write_rows(ctx, rows)
        self._sync_voicemail_user(ext)
        self._log(ext.event, PBXSyncLog.Kind.EXTENSION, ext.number, rows=n)

    @transaction.atomic
    def remove_extension(self, ext) -> None:
        ctx = dp.event_context(ext.event)
        n = DialplanEntry.objects.filter(context=ctx, exten__in=dp.extens_for_extension(ext)).delete()[0]
        VoicemailUser.objects.filter(context=ctx, mailbox=ext.number).delete()
        # devices that were only bound here keep their SIP account but lose the callerid/mailbox
        for b in ext.bindings.select_related("device"):
            if b.device.type in SIP_DEVICE_TYPES and b.device.sip_username:
                self.sync_device(b.device)
        self._log(ext.event, PBXSyncLog.Kind.REMOVE, f"extension {ext.number}", rows=n)

    def sync_event(self, event) -> int:
        from apps.devices.models import Device
        from apps.extensions.models import Extension
        from apps.extensions.services import get_plan

        plan = get_plan(event)
        ctx = dp.event_context(event)
        with transaction.atomic():
            n_rows = self._write_rows(ctx, dp.rows_for_plan(event, plan))
            n = 0
            keep = set(dp.plan_extens(plan))
            for ext in Extension.objects.filter(event=event).active().select_related("event", "owner"):
                self.sync_extension(ext)
                keep |= dp.extens_for_extension(ext)
                n += 1
            DialplanEntry.objects.filter(context=ctx).exclude(exten__in=keep).delete()
            VoicemailUser.objects.filter(context=ctx).exclude(mailbox__in=keep).delete()
            # endpoints for all (non-disabled) SIP devices of the event; prune the rest
            users = set()
            for d in Device.objects.filter(event=event, type__in=SIP_DEVICE_TYPES).exclude(state="disabled"):
                self.sync_device(d)
                users.add(d.sip_username)
            stale = PsEndpoint.objects.filter(accountcode=event.slug[:20]).exclude(id__in=users)
            stale_ids = list(stale.values_list("id", flat=True))
            if stale_ids:
                stale.delete()
                PsAuth.objects.filter(id__in=stale_ids).delete()
                PsAor.objects.filter(id__in=stale_ids).delete()
        self._log(event, PBXSyncLog.Kind.EVENT, event.slug, rows=n_rows,
                  message=f"{n} extensions, {len(users)} endpoints, {len(stale_ids)} pruned")
        self._reload_dialplan()
        return n

    def _reload_dialplan(self):
        """Best effort: make Asterisk re-read the [pet-<slug>] shell contexts (#exec include).

        In agent mode the venue agent reloads its local Asterisk after applying a snapshot; PET only
        tries when an AMI host is configured explicitly and logs (never raises) when it is unreachable.
        """
        if not self.use_ami or not getattr(settings, "PET_PBX_RELOAD_ON_SYNC", True):
            if self.agent_mode:
                log.debug("agent mode: dialplan reload left to the venue agent")
            return
        try:
            with self._ami() as ami:
                ami.reload("pbx_config.so")
        except PBXError as exc:
            log.info("dialplan reload skipped: %s", exc)
        except Exception as exc:  # noqa: BLE001 - reloading is a convenience, provisioning already succeeded
            if not self.agent_mode:
                raise
            log.warning("agent mode: dialplan reload via AMI failed: %s", exc)

    # ------------------------------------------------------------------ status
    def _device_state(self, user: str) -> tuple[str, bool, int]:
        """(state, registered, active_calls) for PJSIP/<user>."""
        if self._ari_offline:
            return self._contact_state(user)
        info = self.ari.endpoint("PJSIP", user)
        registered = info.get("state") == "online"
        calls = len(info.get("channel_ids") or [])
        try:
            raw = self.ari.device_state(f"PJSIP/{user}")
        except ARIError as exc:
            if exc.status not in (404, 501):
                raise
            raw = "UNKNOWN"
        state = _DEVICE_STATE_MAP.get(raw, "unavailable")
        if state == "unavailable" and registered:
            state = "inuse" if calls else "idle"
        return state, registered, calls

    @staticmethod
    def _contact_state(user: str) -> tuple[str, bool, int]:
        """Registration from ``ps_contacts`` (agent mode: rows are fed by the agent's heartbeats)."""
        now = int(datetime.now(UTC).timestamp())
        qs = PsContact.objects.filter(endpoint=user)
        registered = any(c.expiration_time is None or c.expiration_time > now for c in qs)
        return ("idle" if registered else "unavailable"), registered, 0

    def extension_status(self, ext) -> ExtensionStatus:
        users = [ds.split("/", 1)[1].split("@", 1)[0] for _b, ds in dp.dial_targets(ext) if ds.startswith("PJSIP/")]
        if not users:
            return ExtensionStatus(number=ext.number, state="unavailable")
        best, registered, calls = "unavailable", 0, 0
        for u in users:
            st, reg, c = self._device_state(u)
            registered += int(reg)
            calls += c
            if _STATE_RANK[st] > _STATE_RANK[best]:
                best = st
        return ExtensionStatus(number=ext.number, state=best, registered_devices=registered, active_calls=calls)

    def active_channels(self, event=None) -> list[ChannelState]:
        if self._ari_offline:
            raise PBXError("ARI not configured (agent provisioning mode) - live channels are unavailable")
        out = []
        ctx = dp.event_context(event) if event is not None else None
        for ch in self.ari.channels():
            plan = ch.get("dialplan") or {}
            acct = ch.get("accountcode") or ""
            if event is not None and plan.get("context") != ctx and acct != event.slug[:20]:
                continue
            caller = (ch.get("caller") or {}).get("number") or ""
            callee = (ch.get("connected") or {}).get("number") or plan.get("exten") or ""
            out.append(ChannelState(
                id=ch.get("id", ""), caller=caller, callee=callee,
                state=_CHANNEL_STATE_MAP.get(ch.get("state", ""), str(ch.get("state", "")).lower()),
                started_at=ch.get("creationtime"),
                extra={"name": ch.get("name"), "context": plan.get("context"), "exten": plan.get("exten"),
                       "accountcode": acct, "caller_name": (ch.get("caller") or {}).get("name")},
            ))
        return out

    def health(self) -> dict:
        if self._ari_offline:
            return self._agent_health()
        try:
            info = self.ari.info()
        except PBXError as exc:
            return {"ok": False, "backend": self.name, "error": str(exc)}
        system = info.get("system") or {}
        status = info.get("status") or {}
        return {
            "ok": True, "backend": self.name, "version": system.get("version"),
            "entity_id": system.get("entity_id"), "startup_time": status.get("startup_time"),
            "last_reload_time": status.get("last_reload_time"),
        }

    def _agent_health(self) -> dict:
        """Health derived from the venue agent's heartbeats instead of ARI."""
        conn = self._agent_connection()
        out = {"ok": False, "backend": self.name, "mode": "agent", "error": None}
        if conn is None:
            out["error"] = "no PBX connection row"
            return out
        out.update({
            "agent_last_seen": conn.agent_last_seen.isoformat() if conn.agent_last_seen else None,
            "agent_host": conn.agent_host, "agent_software": conn.agent_software,
            "asterisk_ok": conn.agent_asterisk_ok, "applied_version": conn.agent_version,
            "stale": conn.agent_is_stale, "message": conn.agent_message,
        })
        if conn.agent_last_seen is None:
            out["error"] = "no heartbeat from the venue agent yet"
        elif conn.agent_is_stale:
            out["error"] = f"venue agent stale (last seen {conn.agent_last_seen:%Y-%m-%d %H:%M:%S})"
        elif conn.agent_asterisk_ok is False:
            out["error"] = conn.agent_message or "venue agent reports Asterisk down"
        else:
            out["ok"] = True
        return out

    # ------------------------------------------------------------------ call control
    def originate(self, *, event, destination: str, caller_id: str, context: str = "pet-services",
                  variables: dict | None = None, timeout: int = 30) -> str:
        variables = dict(variables or {})
        service = variables.pop("PET_SERVICE", None) or "announce"
        ctx = dp.event_context(event)
        variables.setdefault("PET_EVENT", event.slug)
        variables.setdefault("PET_DESTINATION", destination)
        variables.setdefault("PET_CALLER_ID", caller_id)
        endpoint = f"Local/{destination}@{ctx}"
        if self._ari_offline and not self.use_ami:
            raise PBXError("originate needs ARI or AMI - neither is configured (agent provisioning mode)")
        try:
            ch = self.ari.originate(endpoint=endpoint, context=context, extension=service, priority=1,
                                    caller_id=caller_id, timeout=timeout, variables=variables)
            return ch.get("id") or ""
        except ARIError as exc:
            if exc.status is not None or not self.use_ami:
                raise
            log.warning("ARI unreachable (%s) - falling back to AMI originate", exc)
        try:
            with self._ami() as ami:
                return ami.originate(channel=endpoint, context=context, exten=service, caller_id=caller_id,
                                     timeout=timeout, variables=variables)
        except PBXError as exc:
            raise PBXError(f"originate to {destination} failed via ARI and AMI: {exc}") from exc

    def hangup(self, channel_id: str) -> None:
        self.ari.hangup(channel_id)

    # ------------------------------------------------------------------ MWI
    def set_mwi(self, ext, new_messages: int, old_messages: int = 0) -> None:
        name = dp.mailbox(ext)
        if self._ari_offline:
            log.info("agent mode without ARI: MWI for %s left to the venue Asterisk (app_voicemail)", name)
            return
        try:
            self.ari.set_mailbox(name, new_messages, old_messages)
        except ARIError as exc:
            if exc.status in (404, 501):
                # res_mwi_external / res_ari_mailboxes not loaded (app_voicemail owns MWI then)
                log.warning("MWI via ARI not available (%s); mailbox %s not updated", exc, name)
                return
            raise

    # ------------------------------------------------------------------ export
    def render_dialplan(self, event) -> str:
        from apps.extensions.models import Extension
        from apps.extensions.services import get_plan

        rows = list(dp.rows_for_plan(event, get_plan(event)))
        exts = list(Extension.objects.filter(event=event).active().select_related("event", "owner"))
        for ext in exts:
            rows += dp.rows_for_extension(ext)
        text = dp.render_context(event, rows)
        if any(e.has_ringback_tone for e in exts):
            text += ("\n; Custom ringback tones referenced above via Dial(...,m(<class>)) need the music-on-hold\n"
                     "; classes from render_musiconhold(); see musiconhold.conf (#include pet-moh.conf).\n")
        return text

    def render_musiconhold(self, event) -> str:
        """``musiconhold.conf`` sections for every ready custom ringback tone of ``event``.

        Each tone is processed into its own directory (``MEDIA_ROOT/ringback/processed/<id>/tone.wav``)
        because Asterisk ``mode=files`` plays a *directory*. Write the output to a file that
        ``musiconhold.conf`` includes (``#include pet-moh.conf``) and run ``moh reload``.
        """
        from apps.extensions.models import Extension

        lines = [f"; PET custom ringback tones for event '{event.slug}' - generated "
                 f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC"]
        qs = Extension.objects.filter(event=event).active().filter(
            ringback_tone_status=Extension.RingbackStatus.READY).select_related("event")
        for ext in qs:
            if not ext.ringback_tone_processed:
                continue
            lines += ["", f"[{ext.ringback_class}]", "mode=files",
                      f"directory={self._moh_directory(ext)}", "sort=alpha"]
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _moh_directory(ext) -> str:
        """Absolute directory of the processed tone (storage path when available, else MEDIA_ROOT-based)."""
        f = ext.ringback_tone_processed
        try:
            path = f.path
        except (NotImplementedError, AttributeError):
            path = os.path.join(settings.MEDIA_ROOT, f.name)
        return os.path.dirname(os.path.abspath(path))
