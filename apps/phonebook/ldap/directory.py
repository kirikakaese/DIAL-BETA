"""LDAP view of the event phonebook: DNs, entries and a small per-event cache.

Tree served for every event that is *registration* or *live* and has ``directory_enabled``::

    dc=<slug>,dc=pet                         dcObject / organization
    ou=phonebook,dc=<slug>,dc=pet            organizationalUnit (= services.base_dn)
    cn=<name>+telephoneNumber=<number>,ou=phonebook,dc=<slug>,dc=pet   inetOrgPerson per entry

The multi-valued RDN keeps DNs unique when two extensions share a display name. All ORM access is in
:func:`load_event_directory` / :func:`load_naming_contexts`; the server calls them through ``sync_to_async``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings as dj_settings
from django.db import close_old_connections

logger = logging.getLogger("pet.ldap")

SUFFIX = ("dc=pet",)
BIND_CN = "directory"
PERSON_CLASSES = ["top", "person", "organizationalPerson", "inetOrgPerson"]

_RDN_ESCAPE = {'"', "+", ",", ";", "<", ">", "\\"}


# --------------------------------------------------------------------------- DN helpers (RFC 4514)

def escape_rdn_value(value: str) -> str:
    value = str(value or "").replace("\x00", "\\00")
    out = []
    for i, ch in enumerate(value):
        if ch in _RDN_ESCAPE or (i == 0 and ch in " #") or (i == len(value) - 1 and ch == " "):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _unescape_rdn_value(value: str) -> str:
    out = bytearray()
    raw = value.encode("utf-8")
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x5C and i + 1 < len(raw):
            nxt = raw[i + 1:i + 3]
            if len(nxt) == 2 and all(c in b"0123456789abcdefABCDEF" for c in nxt):
                out.append(int(nxt, 16))
                i += 3
                continue
            out.append(raw[i + 1])
            i += 2
            continue
        out.append(b)
        i += 1
    return out.decode("utf-8", errors="replace")


def _split_unescaped(text: str, separators: str) -> list[str]:
    parts, current, i = [], [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            current.append(text[i:i + 2])
            i += 2
            continue
        if ch in separators:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def parse_dn(dn: str) -> tuple[frozenset[tuple[str, str]], ...]:
    """Normalised DN: one frozenset of ``(attr, value)`` (both lower-cased, unescaped) per RDN, root first.

    Invalid pieces (an RDN without ``=``) are kept as ``("", rdn)`` so they simply never match anything.
    """
    dn = dn.strip()
    if not dn:
        return ()
    rdns = []
    for rdn in _split_unescaped(dn, ",;"):
        avas = set()
        for ava in _split_unescaped(rdn, "+"):
            attr, sep, value = ava.partition("=")
            if not sep:
                avas.add(("", ava.strip().lower()))
            else:
                avas.add((attr.strip().lower(), _unescape_rdn_value(value.strip()).lower()))
        rdns.append(frozenset(avas))
    return tuple(reversed(rdns))


def event_slug_from_dn(dn: str) -> str | None:
    """``<anything>,dc=<slug>,dc=pet`` -> ``slug`` (``None`` when the DN is outside our suffix)."""
    parsed = parse_dn(dn)
    if len(parsed) < 2 or parsed[0] != frozenset({("dc", "pet")}):
        return None
    rdn = parsed[1]
    if len(rdn) != 1:
        return None
    (attr, value), = rdn
    return value if attr == "dc" and value else None


def bind_dn_slug(dn: str) -> str | None:
    """``cn=directory,dc=<slug>,dc=pet`` -> ``slug``; anything else -> ``None``."""
    parsed = parse_dn(dn)
    if len(parsed) != 3 or parsed[2] != frozenset({("cn", BIND_CN)}):
        return None
    return event_slug_from_dn(dn)


def event_dn(slug: str) -> str:
    return f"dc={escape_rdn_value(slug)},dc=pet"


def bind_dn(slug: str) -> str:
    return f"cn={BIND_CN},{event_dn(slug)}"


# --------------------------------------------------------------------------- entries

@dataclass
class Entry:
    dn: str
    attributes: dict[str, list[str]]
    parsed: tuple = field(default_factory=tuple, repr=False)

    def __post_init__(self):
        if not self.parsed:
            self.parsed = parse_dn(self.dn)

    def matches_scope(self, base: tuple, scope: int) -> bool:
        depth = len(self.parsed) - len(base)
        if depth < 0 or self.parsed[:len(base)] != base:
            return False
        if scope == 0:
            return depth == 0
        if scope == 1:
            return depth == 1
        return True

    def select(self, attributes: list[str], types_only: bool = False) -> dict[str, list[str]]:
        """Attribute list as requested by the client (``[]``/``*`` = all user attributes, ``1.1`` = none)."""
        wanted = {a.lower() for a in attributes}
        if not attributes or "*" in wanted:
            chosen = dict(self.attributes)
        else:
            chosen = {k: v for k, v in self.attributes.items() if k.lower() in wanted}
        if types_only:
            return {k: [] for k in chosen}
        return chosen


@dataclass
class EventDirectory:
    slug: str
    name: str
    token: str
    enabled: bool
    entries: list[Entry]
    loaded_at: float = field(default_factory=time.monotonic)

    @property
    def base_dn(self) -> str:
        return event_dn(self.slug)

    def search(self, base: str, scope: int, flt) -> tuple[list[Entry] | None, str]:
        """``(matches, matched_dn)``; ``matches`` is ``None`` when ``base`` does not exist in this tree."""
        parsed = parse_dn(base)
        if not any(e.parsed == parsed for e in self.entries):
            matched = ""
            for e in self.entries:
                if parsed[:len(e.parsed)] == e.parsed and len(e.parsed) > len(parse_dn(matched)):
                    matched = e.dn
            return None, matched
        return [e for e in self.entries if e.matches_scope(parsed, scope) and flt(e.attributes)], base

    def __len__(self):
        return len(self.entries)


def split_name(name: str) -> tuple[str, str]:
    """``"Bob the Builder"`` -> ``("Bob the", "Builder")``; single words become ``("", name)``."""
    parts = name.strip().rsplit(" ", 1)
    if len(parts) == 2 and parts[0]:
        return parts[0], parts[1]
    return "", name.strip()


def person_entry(event, ext, settings, base: str) -> Entry:
    from apps.phonebook.services import entry_name

    name = entry_name(ext)
    given, sn = split_name(name)
    attrs: dict[str, list[str]] = {
        "objectClass": list(PERSON_CLASSES),
        "cn": [name],
        "sn": [sn or name],
        "displayName": [name],
        "telephoneNumber": [ext.number],
        "mobile": [ext.number],
        "uid": [ext.number],
        "title": [str(ext.get_type_display())],
        "o": [event.name],
    }
    if given:
        attrs["givenName"] = [given]
    description = []
    if settings.show_location and ext.location_hint:
        description.append(ext.location_hint)
        attrs["l"] = [ext.location_hint]
    if ext.description:
        description.append(ext.description)
    if description:
        attrs["description"] = [" - ".join(description)]
    category = settings.category_for(ext.number) if settings.categories else ""
    if category:
        attrs["ou"] = [category]
    dn = f"cn={escape_rdn_value(name)}+telephoneNumber={escape_rdn_value(ext.number)},{base}"
    return Entry(dn, attrs)


def build_entries(event, exts, settings) -> list[Entry]:
    from apps.phonebook.services import base_dn

    root = event_dn(event.slug)
    base = base_dn(event)
    out = [
        Entry(root, {"objectClass": ["top", "dcObject", "organization"], "dc": [event.slug],
                     "o": [event.name]}),
        Entry(base, {"objectClass": ["top", "organizationalUnit"], "ou": ["phonebook"],
                     "description": [f"{event.name} phonebook"]}),
    ]
    out += [person_entry(event, e, settings, base) for e in exts]
    return out


def _servable_events():
    from apps.events.models import Event

    return Event.objects.filter(state__in=[Event.State.REGISTRATION, Event.State.LIVE])


def load_event_directory(slug: str) -> EventDirectory | None:
    """ORM: build the tree of one event; ``None`` for unknown slugs, ``enabled=False`` for hidden ones."""
    from apps.phonebook import services

    close_old_connections()
    event = _servable_events().filter(slug=slug).first()
    if event is None:
        return None
    settings = services.get_settings(event)
    if not settings.directory_enabled:
        return EventDirectory(slug, event.name, settings.directory_token, False, [])
    entries = build_entries(event, services.entries(event), settings)
    logger.debug("ldap: loaded %d entries for %s", len(entries) - 2, slug)
    return EventDirectory(slug, event.name, settings.directory_token, True, entries)


def load_naming_contexts() -> list[str]:
    """ORM: ``dc=<slug>,dc=pet`` for every servable event with the directory enabled."""
    from apps.phonebook.models import PhonebookSettings

    close_old_connections()
    slugs = list(_servable_events().order_by("slug").values_list("slug", flat=True))
    disabled = set(PhonebookSettings.objects.filter(event__slug__in=slugs, directory_enabled=False)
                   .values_list("event__slug", flat=True))
    return [event_dn(s) for s in slugs if s not in disabled]


class DirectoryCache:
    """TTL cache in front of :func:`load_event_directory` (``PET_LDAP_CACHE_SECONDS``, default 30)."""

    def __init__(self, ttl: float | None = None):
        self.ttl = float(getattr(dj_settings, "PET_LDAP_CACHE_SECONDS", 30) if ttl is None else ttl)
        self._events: dict[str, tuple[float, EventDirectory | None]] = {}
        self._contexts: tuple[float, list[str]] | None = None

    def _fresh(self, stamp: float) -> bool:
        return time.monotonic() - stamp < self.ttl

    def get_event(self, slug: str, loader=load_event_directory) -> EventDirectory | None:
        hit = self._events.get(slug)
        if hit and self._fresh(hit[0]):
            return hit[1]
        directory = loader(slug)
        self._events[slug] = (time.monotonic(), directory)
        return directory

    def naming_contexts(self, loader=load_naming_contexts) -> list[str]:
        if self._contexts and self._fresh(self._contexts[0]):
            return self._contexts[1]
        contexts = loader()
        self._contexts = (time.monotonic(), contexts)
        return contexts

    def clear(self):
        self._events.clear()
        self._contexts = None
