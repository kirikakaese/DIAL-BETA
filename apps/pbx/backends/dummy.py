"""In-memory PBX adapter for tests/dev.

Records every call so other apps' tests can assert on it:

- ``extensions``: ``{ext.pk: number}`` of synced extensions
- ``devices``: ``{device.pk: sip_username}`` of synced devices
- ``originated``: list of dicts (``id``, ``destination``, ``caller_id``, ``context``, ``variables``)
- ``hungup``: list of channel ids
- ``mwi``: list of ``(number, new, old)`` tuples
- ``channels``: list of dicts / ``ChannelState`` you may populate; returned by ``active_channels``
- ``statuses``: ``{number: ExtensionStatus}`` overrides for ``extension_status``
- ``removed_extensions`` / ``removed_devices``: pks passed to the remove_* methods
- ``synced_events``: event slugs passed to ``sync_event``
"""
from dataclasses import asdict

from apps.pbx.base import ChannelState, ExtensionStatus, PBXAdapter


class DummyPBX(PBXAdapter):
    name = "dummy"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.extensions = {}
        self.devices = {}
        self.originated = []
        self.hungup = []
        self.mwi = []
        self.channels = []
        self.statuses = {}
        self.removed_extensions = []
        self.removed_devices = []
        self.synced_events = []
        self.healthy = True

    def reset(self):
        self.__init__()

    # --- provisioning ------------------------------------------------------
    def sync_extension(self, ext):
        self.extensions[str(ext.pk)] = ext.number
        for b in ext.bindings.filter(is_active=True).select_related("device"):
            self.sync_device(b.device)

    def remove_extension(self, ext):
        self.extensions.pop(str(ext.pk), None)
        self.removed_extensions.append(str(ext.pk))

    def sync_device(self, device):
        self.devices[str(device.pk)] = device.sip_username

    def remove_device(self, device):
        self.devices.pop(str(device.pk), None)
        self.removed_devices.append(str(device.pk))

    def sync_event(self, event):
        self.synced_events.append(event.slug)
        return super().sync_event(event)

    # --- status ------------------------------------------------------------
    def extension_status(self, ext):
        return self.statuses.get(ext.number) or ExtensionStatus(number=ext.number, state="idle")

    def active_channels(self, event=None):
        out = []
        for c in self.channels:
            ch = c if isinstance(c, ChannelState) else ChannelState(**c)
            if event is not None and ch.extra.get("event") not in (None, event.slug):
                continue
            out.append(ch)
        return out

    def health(self):
        return {"ok": self.healthy, "backend": self.name}

    # --- call control ------------------------------------------------------
    def originate(self, *, event, destination, caller_id, context="pet-services", variables=None, timeout=30):
        cid = f"dummy-{len(self.originated) + 1}"
        self.originated.append({"id": cid, "destination": destination, "caller_id": caller_id,
                                "context": context, "variables": variables or {}, "timeout": timeout,
                                "event": event.slug if event is not None else None})
        self.channels.append(ChannelState(id=cid, caller=caller_id, callee=destination, state="ringing",
                                          extra={"event": event.slug if event is not None else None}))
        return cid

    def hangup(self, channel_id):
        self.hungup.append(channel_id)
        self.channels = [c for c in self.channels
                         if (c.id if isinstance(c, ChannelState) else c.get("id")) != channel_id]

    # --- MWI / export ------------------------------------------------------
    def set_mwi(self, ext, new_messages, old_messages=0):
        self.mwi.append((ext.number, new_messages, old_messages))

    def render_dialplan(self, event):
        from apps.extensions.models import Extension
        from apps.extensions.services import get_plan
        from apps.pbx import dialplan as dp

        rows = list(dp.rows_for_plan(event, get_plan(event)))
        for ext in Extension.objects.filter(event=event).active():
            rows += dp.rows_for_extension(ext)
        return dp.render_context(event, rows)

    def render_musiconhold(self, event):
        """No music-on-hold config for the in-memory backend."""
        return ""

    def snapshot(self) -> dict:
        """Plain-dict view of the recorded state (handy for assertions)."""
        return {
            "extensions": dict(self.extensions), "devices": dict(self.devices),
            "originated": list(self.originated), "hungup": list(self.hungup), "mwi": list(self.mwi),
            "channels": [asdict(c) if isinstance(c, ChannelState) else dict(c) for c in self.channels],
        }
