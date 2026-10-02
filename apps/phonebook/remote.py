"""Remote phonebook for desk phones and the DECT OMM (feature #9): vendor XML directories.

Desk phones cannot log in, so the per-event ``PhonebookSettings.directory_token`` is the credential -
a path segment of the directory URL (``/e/<slug>/phonebook/remote/<token>/<vendor>.xml``). The same
document is served under a device's autoprovisioning token (``/prov/<token>/phonebook.xml``) so a
provisioned phone finds its directory without the event-wide secret appearing in its config file.

Renderers (``VENDORS``):

* ``snom``        ``<SnomIPPhoneDirectory>`` (XML minibrowser)
* ``yealink``     ``<YealinkIPPhoneDirectory>`` (remote phonebook)
* ``grandstream`` ``<AddressBook><Contact>`` (XML phonebook, fetched as ``.../phonebook.xml``)
* ``cisco``       ``<CiscoIPPhoneDirectory>`` (XML directory service)
* ``mitel``       ``<IPPhoneDirectory>`` for the SIP-DECT OMM "XML application / corporate directory"
* ``generic``     plain ``<IPPhoneDirectory>`` for everything else that speaks the common schema

All of them honour ``?q=`` / ``?search=`` / ``?name=`` (handsets query with a name fragment).
"""
from __future__ import annotations

import hmac
import re
from urllib.parse import urlsplit

from django.conf import settings as dj_settings
from django.urls import reverse

from . import services
from .models import PhonebookSettings

VENDORS = ("snom", "yealink", "grandstream", "cisco", "mitel", "generic")
VENDOR_LABELS = {
    "snom": "Snom", "yealink": "Yealink", "grandstream": "Grandstream", "cisco": "Cisco",
    "mitel": "Mitel SIP-DECT OMM / Aastra", "generic": "Generic IPPhoneDirectory",
}
CONTENT_TYPE = "text/xml; charset=utf-8"
PROV_FILENAME = "phonebook.xml"
SEARCH_PARAMS = ("q", "search", "name")
LDAP_DEFAULT_PORT = 3890

_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# --------------------------------------------------------------------------- helpers

def _x(value) -> str:
    """XML-escape text and drop control characters phones choke on."""
    value = _XML_ILLEGAL.sub("", str(value if value is not None else ""))
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def public_base() -> str:
    return (getattr(dj_settings, "PET_PUBLIC_URL", "") or "").rstrip("/")


def servable(event, settings: PhonebookSettings | None = None) -> bool:
    """Phones may only pull the directory while the event is open (registration/live) and the orga has
    not switched the remote directory off - the same rule the LDAP server applies."""
    from apps.core.features import enabled
    from apps.events.models import Event

    if event is None or event.state not in (Event.State.REGISTRATION, Event.State.LIVE):
        return False
    if not enabled("phonebook", event):
        return False
    settings = settings or services.get_settings(event)
    return bool(settings.directory_enabled)


def token_ok(settings: PhonebookSettings, token: str) -> bool:
    """Constant-time token check; the directory must also be enabled."""
    if not settings.directory_enabled or not settings.directory_token or not token:
        return False
    return hmac.compare_digest(str(token), str(settings.directory_token))


def search_term(params) -> str | None:
    """``?q=`` / ``?search=`` / ``?name=`` - the first non-empty one wins."""
    for key in SEARCH_PARAMS:
        value = (params.get(key) or "").strip()
        if value:
            return value
    return None


def display_name(ext) -> str:
    """``entry_name``; trunk blocks additionally carry their range (``Foo PBX (4700–4799)``)."""
    name = services.entry_name(ext)
    label = ext.number_label
    if label != ext.number and label not in name:
        return f"{name} ({label})"
    return name


def _split_name(name: str) -> tuple[str, str]:
    """``(first, last)`` the way the LDIF export does it: first word / rest; single words are the last name."""
    parts = name.split(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", name)


# --------------------------------------------------------------------------- renderers

def _ip_phone_directory(root: str, event, exts, *, title: bool = True, prompt: bool = True) -> str:
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f"<{root}>"]
    if title:
        lines.append(f"  <Title>{_x(event.name)}</Title>")
    if prompt:
        lines.append("  <Prompt>Select an entry</Prompt>")
    for e in exts:
        lines += ["  <DirectoryEntry>", f"    <Name>{_x(display_name(e))}</Name>",
                  f"    <Telephone>{_x(e.number)}</Telephone>", "  </DirectoryEntry>"]
    lines.append(f"</{root}>")
    return "\n".join(lines) + "\n"


def render_snom(event, exts) -> str:
    return _ip_phone_directory("SnomIPPhoneDirectory", event, exts)


def render_yealink(event, exts) -> str:
    # Yealink's remote phonebook only knows Title + DirectoryEntry; no Prompt element.
    return _ip_phone_directory("YealinkIPPhoneDirectory", event, exts, prompt=False)


def render_cisco(event, exts) -> str:
    return _ip_phone_directory("CiscoIPPhoneDirectory", event, exts)


def render_mitel(event, exts) -> str:
    return _ip_phone_directory("IPPhoneDirectory", event, exts)


def render_generic(event, exts) -> str:
    return _ip_phone_directory("IPPhoneDirectory", event, exts)


def render_grandstream(event, exts) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<AddressBook>"]
    for e in exts:
        first, last = _split_name(display_name(e))
        lines += [
            "  <Contact>",
            f"    <FirstName>{_x(first)}</FirstName>",
            f"    <LastName>{_x(last)}</LastName>",
            '    <Phone type="Work">',
            f"      <phonenumber>{_x(e.number)}</phonenumber>",
            "      <accountindex>0</accountindex>",
            "    </Phone>",
            "  </Contact>",
        ]
    lines.append("</AddressBook>")
    return "\n".join(lines) + "\n"


RENDERERS = {
    "snom": render_snom, "yealink": render_yealink, "grandstream": render_grandstream,
    "cisco": render_cisco, "mitel": render_mitel, "generic": render_generic,
}


def render_directory(event, vendor: str, q: str | None = None, exts=None) -> str:
    """The vendor XML for ``event``'s phonebook entries (optionally filtered by ``q``)."""
    render = RENDERERS[vendor]
    exts = services.entries(event, q=q) if exts is None else exts
    return render(event, exts)


# --------------------------------------------------------------------------- URLs

def directory_url(event, vendor: str, settings: PhonebookSettings | None = None) -> str:
    """Absolute ``/e/<slug>/phonebook/remote/<token>/<vendor>.xml`` rooted at ``PET_PUBLIC_URL``."""
    settings = settings or services.get_settings(event)
    return public_base() + reverse("phonebook:remote_directory",
                                   args=[event.slug, settings.directory_token, vendor])


def directory_urls(event, settings: PhonebookSettings | None = None) -> dict[str, str]:
    settings = settings or services.get_settings(event)
    return {vendor: directory_url(event, vendor, settings) for vendor in VENDORS}


def prov_directory_url(device) -> str:
    """``/prov/<provisioning token>/phonebook.xml`` for a device, or ``""`` when it cannot be served."""
    if not device.provisioning_token or not servable(device.event):
        return ""
    return public_base() + reverse("prov:phonebook", args=[device.provisioning_token])


def vendor_for_device(device, requested: str | None = None) -> str:
    """Renderer for a provisioned device: ``?vendor=`` override, else its profile's vendor, else generic."""
    if requested in RENDERERS:
        return requested
    profile = device.provisioning_profile
    if profile is not None and profile.vendor in RENDERERS:
        return profile.vendor
    return "generic"


def ldap_info(event) -> dict:
    """What to type into a phone's LDAP directory settings (the LDAP server is ``manage.py pet_ldap``).

    ``PET_LDAP_HOST`` is the *bind* address of that server; wildcard binds (``0.0.0.0`` / ``::``) are not
    reachable names, so the public hostname of ``PET_PUBLIC_URL`` is shown instead.
    """
    host = getattr(dj_settings, "PET_LDAP_HOST", "") or ""
    if host in ("0.0.0.0", "::", "*"):
        host = ""
    host = host or urlsplit(public_base()).hostname or "localhost"
    return {
        "host": host,
        "port": int(getattr(dj_settings, "PET_LDAP_PORT", LDAP_DEFAULT_PORT)),
        "base_dn": services.base_dn(event),
        "bind_dn": f"cn=directory,dc={event.slug},dc=pet",
        "name_attributes": "cn sn",
        "number_attribute": "telephoneNumber",
    }


def directory_info(event, settings: PhonebookSettings | None = None, *, with_token: bool = True) -> dict:
    """``{enabled, token, urls, ldap}`` for the orga UI, API and CLI."""
    settings = settings or services.get_settings(event)
    ldap = ldap_info(event)
    if with_token:
        ldap["password"] = settings.directory_token
    return {
        "enabled": settings.directory_enabled,
        "token": settings.directory_token if with_token else None,
        "urls": directory_urls(event, settings),
        "ldap": ldap,
    }
