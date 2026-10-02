"""Minimal Asterisk REST Interface (ARI) client built on ``requests``.

Only the handful of resources DIAL needs: channels, endpoints, device states,
mailboxes and ``/asterisk/info``. All transport failures and HTTP errors are
turned into :class:`ARIError` (a :class:`~apps.pbx.base.PBXError`).
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import requests

from apps.pbx.base import PBXError

log = logging.getLogger("dial.pbx.ari")


class ARIError(PBXError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ARIClient:
    def __init__(self, base_url: str, user: str, password: str, *, app: str = "dial", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.app = app
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.session.headers["Accept"] = "application/json"

    # --- transport -----------------------------------------------------------
    def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self.session.request(method, url, params=params, json=json, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ARIError(f"ARI {method} {path} failed: {exc.__class__.__name__}: {exc}") from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except ValueError:
                detail = (resp.text or "")[:200]
            raise ARIError(f"ARI {method} {path} -> HTTP {resp.status_code}: {detail or resp.reason}",
                           status=resp.status_code)
        if resp.status_code == 204 or not (resp.content or b"").strip():
            return None
        try:
            return resp.json()
        except ValueError:
            raise ARIError(f"ARI {method} {path}: invalid JSON response")

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path, *, params=None, json=None):
        return self.request("POST", path, params=params, json=json)

    def put(self, path, *, params=None, json=None):
        return self.request("PUT", path, params=params, json=json)

    def delete(self, path, **params):
        return self.request("DELETE", path, params=params or None)

    # --- resources -----------------------------------------------------------
    def info(self) -> dict:
        return self.get("/asterisk/info") or {}

    def channels(self) -> list[dict]:
        return self.get("/channels") or []

    def originate(self, *, endpoint: str, context: str, extension: str, priority: int = 1,
                  caller_id: str = "", timeout: int = 30, variables: dict | None = None,
                  channel_id: str | None = None) -> dict:
        params = {
            "endpoint": endpoint, "context": context, "extension": extension, "priority": priority,
            "timeout": timeout,
        }
        if caller_id:
            params["callerId"] = caller_id
        if channel_id:
            params["channelId"] = channel_id
        body = {"variables": {k: str(v) for k, v in (variables or {}).items()}}
        return self.post("/channels", params=params, json=body) or {}

    def hangup(self, channel_id: str, reason: str = "normal") -> None:
        self.delete(f"/channels/{quote(channel_id, safe='')}", reason=reason)

    def endpoint(self, tech: str, resource: str) -> dict:
        return self.get(f"/endpoints/{tech}/{quote(resource, safe='')}") or {}

    def endpoints(self, tech: str = "PJSIP") -> list[dict]:
        return self.get(f"/endpoints/{tech}") or []

    def device_state(self, device_name: str) -> str:
        data = self.get(f"/deviceStates/{quote(device_name, safe='')}") or {}
        return data.get("state", "UNKNOWN")

    def set_mailbox(self, name: str, new_messages: int, old_messages: int = 0) -> None:
        self.put(f"/mailboxes/{quote(name, safe='')}",
                 params={"newMessages": int(new_messages), "oldMessages": int(old_messages)})
