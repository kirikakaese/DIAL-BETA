"""Vendor-specific softphone provisioning documents served under ``/prov/<token>/<client>.xml``.

Both formats are fetched by the softphone itself after scanning a QR code, so the documents carry the
SIP password in clear text - the per-device provisioning token in the URL is the only secret protecting
them (same model as the desk-phone configs in :mod:`apps.devices.prov_views`).

* **Linphone** remote provisioning: ``lpconfig`` XML; QR content ``linphone-config:<url>``.
* **Acrobits** (Groundwire, Cloud Softphone, Acrobits Softphone): ``<account>`` XML; QR content is the URL.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from django.utils import timezone

from .models import Device

XML_CONTENT_TYPE = "application/xml; charset=utf-8"

# Linphone/Acrobits register over classic SIP transports only; a ``wss`` device gets TLS as closest match.
_SOFTPHONE_TRANSPORTS = {"udp": "udp", "tcp": "tcp", "tls": "tls", "wss": "tls"}


def softphone_transport(device: Device) -> str:
    return _SOFTPHONE_TRANSPORTS.get(device.sip_transport, "udp")


def _entries(section: str, entries: dict[str, object]) -> str:
    rows = "\n".join(f'    <entry name="{name}">{escape(str(value))}</entry>' for name, value in entries.items())
    return f'  <section name="{section}">\n{rows}\n  </section>'


def render_linphone(device: Device) -> str:
    """lpconfig XML for Linphone's *Fetch remote configuration* assistant."""
    transport = softphone_transport(device)
    domain = device.sip_domain
    proxy = f"<sip:{domain};transport={transport}>"
    identity = f'"{device.sip_display_name}" <sip:{device.sip_username}@{domain}>'
    sections = [
        _entries("proxy_0", {
            "reg_proxy": proxy,
            "reg_route": proxy,
            "reg_identity": identity,
            "reg_expires": 600,
            "reg_sendregister": 1,
            "publish": 0,
            "dial_escape_plus": 0,
            "quality_reporting_enabled": 0,
        }),
        _entries("auth_info_0", {
            "username": device.sip_username,
            "passwd": device.sip_password,
            "realm": domain,
            "domain": domain,
        }),
        _entries("sip", {
            "default_proxy": 0,
            # -1 = enabled on a random local port, 0 = transport disabled
            "sip_port": -1 if transport == "udp" else 0,
            "sip_tcp_port": -1 if transport == "tcp" else 0,
            "sip_tls_port": -1 if transport == "tls" else 0,
            "guess_hostname": 1,
            "register_only_when_network_is_up": 1,
            "use_info": 0,
            "use_rfc2833": 1,
        }),
        _entries("misc", {"transient_provisioning": 0}),
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config xmlns="http://www.linphone.org/xsds/lpconfig.xsd" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.linphone.org/xsds/lpconfig.xsd lpconfig.xsd">\n'
        + "\n".join(sections) + "\n</config>\n"
    )


def render_acrobits(device: Device) -> str:
    """``<account>`` XML consumed by Groundwire / Cloud Softphone / Acrobits Softphone after a QR scan."""
    ext = device.primary_extension
    title = f"PET {ext.number}" if ext else f"PET {device.event.name}"
    fields = {
        "title": title,
        "username": device.sip_username,
        "password": device.sip_password,
        "host": device.sip_domain,
        "transport": softphone_transport(device),
        "displayName": device.sip_display_name,
    }
    body = "\n".join(f"  <{tag}>{escape(str(value))}</{tag}>" for tag, value in fields.items())
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<account>\n{body}\n</account>\n'


RENDERERS = {"linphone": render_linphone, "acrobits": render_acrobits}


def render_softphone(device: Device, client: str) -> tuple[str, str]:
    """``(body, content_type)``; records the fetch in ``device.config`` like desk-phone provisioning does."""
    body = RENDERERS[client](device)
    cfg = dict(device.config or {})
    cfg["last_provisioned_at"] = timezone.now().isoformat(timespec="seconds")
    cfg["last_provisioned_client"] = client
    Device.objects.filter(pk=device.pk).update(config=cfg)
    return body, XML_CONTENT_TYPE
