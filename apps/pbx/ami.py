"""Tiny Asterisk Manager Interface (AMI) client.

Socket based, synchronous, no third-party dependency. Used only as an optional
fallback for ``originate`` (when ARI is unreachable), for ``DeviceStateList``
and to trigger ``dialplan reload`` after an event's shell context changed.

    with AMIClient(host, port, user, password) as ami:
        ami.action("Ping")
"""
from __future__ import annotations

import logging
import socket
import uuid

from apps.pbx.base import PBXError

log = logging.getLogger("dial.pbx.ami")


class AMIError(PBXError):
    pass


class AMIClient:
    def __init__(self, host: str, port: int = 5038, user: str = "", password: str = "", timeout: float = 5.0):
        self.host, self.port, self.user, self.password, self.timeout = host, port, user, password, timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    # --- connection ------------------------------------------------------------
    def connect(self) -> AMIClient:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._read_line()  # banner "Asterisk Call Manager/x.y"
            resp = self.action("Login", Username=self.user, Secret=self.password)
        except OSError as exc:
            self.close()
            raise AMIError(f"AMI connect to {self.host}:{self.port} failed: {exc}") from exc
        if resp.get("Response") != "Success":
            self.close()
            raise AMIError(f"AMI login failed: {resp.get('Message', 'unknown error')}")
        return self

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        try:
            if self._sock is not None:
                self.action("Logoff")
        except Exception:  # noqa: BLE001 - best effort
            pass
        self.close()

    # --- protocol ------------------------------------------------------------------
    def _read_line(self) -> str:
        assert self._sock is not None
        while b"\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise AMIError("AMI connection closed")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\r\n")
        return line.decode("utf-8", "replace")

    def _read_packet(self) -> dict:
        pkt: dict[str, str] = {}
        while True:
            line = self._read_line()
            if line == "":
                if pkt:
                    return pkt
                continue
            key, sep, value = line.partition(":")
            if sep:
                key = key.strip()
                value = value.strip()
                if key in pkt:  # repeated keys (Variable: ...) -> join
                    pkt[key] = f"{pkt[key]}\n{value}"
                else:
                    pkt[key] = value
            else:
                pkt.setdefault("_raw", "")
                pkt["_raw"] += line + "\n"

    def action(self, name: str, **params) -> dict:
        """Send an action, return its response packet. Values may be lists (repeated header)."""
        if self._sock is None:
            raise AMIError("AMI not connected")
        action_id = uuid.uuid4().hex[:12]
        lines = [f"Action: {name}", f"ActionID: {action_id}"]
        for k, v in params.items():
            if v is None:
                continue
            for item in (v if isinstance(v, (list, tuple)) else [v]):
                lines.append(f"{k}: {item}")
        try:
            self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        except OSError as exc:
            raise AMIError(f"AMI send failed: {exc}") from exc
        while True:
            pkt = self._read_packet()
            if pkt.get("ActionID") == action_id and "Response" in pkt:
                return pkt
            # unsolicited events are ignored

    def action_list(self, name: str, complete_event: str, **params) -> tuple[dict, list[dict]]:
        """Actions that answer with an event list terminated by ``complete_event``."""
        first = self.action(name, **params)
        events: list[dict] = []
        if first.get("Response") != "Success":
            return first, events
        while True:
            pkt = self._read_packet()
            if pkt.get("Event") == complete_event:
                return first, events
            if "Event" in pkt:
                events.append(pkt)

    # --- helpers -------------------------------------------------------------------
    def ping(self) -> bool:
        return self.action("Ping").get("Response") == "Success"

    def originate(self, *, channel: str, context: str, exten: str, priority: int = 1, caller_id: str = "",
                  timeout: int = 30, variables: dict | None = None, channel_id: str | None = None) -> str:
        cid = channel_id or f"dial-{uuid.uuid4().hex[:12]}"
        resp = self.action(
            "Originate", Channel=channel, Context=context, Exten=exten, Priority=priority,
            CallerID=caller_id or None, Timeout=timeout * 1000, Async="true", ChannelId=cid,
            Variable=[f"{k}={v}" for k, v in (variables or {}).items()],
        )
        if resp.get("Response") != "Success":
            raise AMIError(f"AMI Originate failed: {resp.get('Message', 'unknown error')}")
        return cid

    def hangup(self, channel: str) -> None:
        resp = self.action("Hangup", Channel=channel)
        if resp.get("Response") != "Success":
            raise AMIError(f"AMI Hangup failed: {resp.get('Message', 'unknown error')}")

    def device_state_list(self) -> dict[str, str]:
        """``{"PJSIP/demo-aaaa": "NOT_INUSE", ...}``"""
        _, events = self.action_list("DeviceStateList", "DeviceStateListComplete")
        return {e.get("Device", ""): e.get("State", "UNKNOWN") for e in events if e.get("Device")}

    def reload(self, module: str = "pbx_config.so") -> bool:
        return self.action("Reload", Module=module).get("Response") == "Success"
