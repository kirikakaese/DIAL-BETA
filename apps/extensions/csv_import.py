"""Bulk CSV import of extensions (and, optionally, their owners) for orga.

Three steps, shared by portal, API and CLI::

    rows = parse_csv(text)                       # delimiter/BOM tolerant, header-driven
    plan = preview(event, rows, create_users=…)  # what would happen, row by row
    result = apply(event, rows, actor, create_users=…)

Every extension is created through :func:`apps.extensions.services.register` with ``force_active=True``
(numbers activate immediately; taken/blocked/emergency numbers still fail). Each row is applied in its own
transaction so one bad row never rolls back the others.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.core.audit import log
from apps.events.models import EventMembership, UserGroup

from . import services
from .models import ExtensionType

# Canonical column names; anything else is ignored (and reported).
COLUMNS = ["number", "type", "display_name", "email", "username", "description", "location", "in_phonebook",
           "group", "role"]
# Friendly header aliases -> canonical column
ALIASES = {
    "extension": "number", "ext": "number", "nummer": "number",
    "name": "display_name", "displayname": "display_name", "caller_id": "display_name",
    "e_mail": "email", "mail": "email", "owner": "email",
    "nick": "username", "nickname": "username", "user": "username",
    "location_hint": "location", "ort": "location",
    "phonebook": "in_phonebook", "public": "in_phonebook",
    "usergroup": "group", "user_group": "group",
}
TRUE_VALUES = {"1", "yes", "y", "true", "on", "x", "ja"}
FALSE_VALUES = {"0", "no", "n", "false", "off", "nein"}
ROLE_VALUES = [EventMembership.Role.USER, EventMembership.Role.HELPDESK, EventMembership.Role.ORGA]
_ROLE_RANK = {"user": 0, "helpdesk": 1, "orga": 2, "admin": 3}

ACTION_CREATE = "create"
ACTION_SKIP_TAKEN = "skip-taken"
ACTION_ERROR = "error"
ACTION_CREATED = "created"


class CSVImportError(ValueError):
    """The file as a whole is unusable (no header, no ``number`` column, undecodable...)."""


@dataclass
class Row:
    """One data line of the CSV, normalised. ``errors`` holds parse-time problems of this line."""

    line: int
    number: str
    type: str = ExtensionType.DECT
    display_name: str = ""
    email: str = ""
    username: str = ""
    description: str = ""
    location: str = ""
    in_phonebook: bool | None = None  # None = model default (True)
    group: str = ""
    role: str = ""
    errors: list[str] = field(default_factory=list)


class ParsedRows(list):
    """``list[Row]`` that also remembers the header it was parsed from."""

    columns: list[str]
    unknown_columns: list[str]

    def __init__(self, rows=(), *, columns=None, unknown_columns=None):
        super().__init__(rows)
        self.columns = list(columns or [])
        self.unknown_columns = list(unknown_columns or [])


# --- parsing ------------------------------------------------------------------

def decode_upload(data: bytes) -> str:
    """Bytes of an uploaded file -> text (UTF-8 with or without BOM, falling back to Latin-1)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _sniff_delimiter(header_line: str) -> str:
    counts = {d: header_line.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _normalise_header(name: str) -> str:
    key = (name or "").strip().strip("\ufeff").lower().replace(" ", "_").replace("-", "_")
    return ALIASES.get(key, key)


def _parse_bool(raw: str) -> bool | None:
    v = (raw or "").strip().lower()
    if v == "":
        return None
    if v in TRUE_VALUES:
        return True
    if v in FALSE_VALUES:
        return False
    raise ValueError(raw)


def parse_csv(text: str) -> ParsedRows:
    """Parse CSV text into :class:`Row` objects. Raises :class:`CSVImportError` when unusable."""
    if isinstance(text, bytes):
        text = decode_upload(text)
    text = (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        raise CSVImportError(_("The file is empty."))
    delimiter = _sniff_delimiter(lines[0])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True)
    header: list[str] | None = None
    rows = ParsedRows()
    for lineno, raw in enumerate(reader, start=1):
        if not any(cell.strip() for cell in raw):
            continue
        if header is None:
            header = [_normalise_header(h) for h in raw]
            rows.columns = header
            rows.unknown_columns = sorted({h for h in header if h not in COLUMNS and h})
            if "number" not in header:
                raise CSVImportError(_("The header row must contain a 'number' column."))
            continue
        cells = dict(zip(header, [c.strip() for c in raw], strict=False))
        row = Row(line=lineno, number=cells.get("number", "").replace(" ", ""))
        if not row.number:
            row.errors.append(_("Missing number."))
        ext_type = (cells.get("type") or ExtensionType.DECT).lower()
        if ext_type not in ExtensionType.values:
            row.errors.append(_("Unknown extension type '%(t)s'.") % {"t": ext_type})
        row.type = ext_type
        row.display_name = cells.get("display_name", "")[:60]
        row.email = cells.get("email", "").lower()
        row.username = cells.get("username", "")[:64]
        row.description = cells.get("description", "")[:200]
        row.location = cells.get("location", "")[:120]
        try:
            row.in_phonebook = _parse_bool(cells.get("in_phonebook", ""))
        except ValueError:
            row.errors.append(_("Invalid value '%(v)s' for in_phonebook (use yes/no).") % {
                "v": cells.get("in_phonebook", "")})
        row.group = cells.get("group", "").lower()
        role = cells.get("role", "").lower()
        if role and role not in ROLE_VALUES:
            row.errors.append(_("Unknown role '%(r)s' (use user, helpdesk or orga).") % {"r": role})
        row.role = role
        if row.email:
            try:
                validate_email(row.email)
            except ValidationError:
                row.errors.append(_("Invalid e-mail address '%(e)s'.") % {"e": row.email})
        rows.append(row)
    if header is None:
        raise CSVImportError(_("The file is empty."))
    return rows


def sample_csv() -> str:
    """Header plus two example rows, ready for download."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    w.writerow(["4242", "dect", "Alice", "alice@example.org", "alice", "Infodesk phone", "Hall 2, infodesk",
                "yes", "angels", "user"])
    w.writerow(["4300", "sip", "NOC hotline", "", "", "Network operations", "NOC container", "no", "", ""])
    return buf.getvalue()


# --- preview --------------------------------------------------------------------

@dataclass
class PlanRow:
    row: Row
    action: str = ACTION_CREATE
    messages: list[str] = field(default_factory=list)
    owner: User | None = None
    owner_exists: bool = False
    create_user: bool = False
    ownerless: bool = False
    new_username: str = ""
    extension_id: str | None = None

    @property
    def owner_label(self) -> str:
        if self.owner is not None:
            return self.owner.email
        if self.create_user:
            return self.row.email
        return ""

    def as_dict(self) -> dict:
        return {
            "line": self.row.line,
            "number": self.row.number,
            "type": self.row.type,
            "display_name": self.row.display_name,
            "owner": self.owner_label or None,
            "owner_exists": self.owner_exists,
            "create_user": self.create_user,
            "new_username": self.new_username or None,
            "ownerless": self.ownerless,
            "group": self.row.group or None,
            "role": self.row.role or None,
            "action": self.action,
            "messages": [str(m) for m in self.messages],
            "extension_id": self.extension_id,
        }


@dataclass
class ImportPlan:
    rows: list[PlanRow]
    unknown_columns: list[str] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for r in self.rows if r.action == action)

    @property
    def creatable(self) -> int:
        return self.count(ACTION_CREATE)

    @property
    def skipped(self) -> int:
        return self.count(ACTION_SKIP_TAKEN)

    @property
    def errors(self) -> int:
        return self.count(ACTION_ERROR)

    @property
    def new_users(self) -> int:
        return len({r.row.email for r in self.rows if r.create_user and r.action == ACTION_CREATE})

    def as_list(self) -> list[dict]:
        return [r.as_dict() for r in self.rows]


def _unique_username(base: str) -> str:
    base = (base or "user").strip()[:60] or "user"
    candidate = base
    i = 1
    while User.objects.filter(username__iexact=candidate).exists():
        i += 1
        candidate = f"{base}{i}"
    return candidate


def _resolve_owner(prow: PlanRow, *, create_users: bool, allow_ownerless: bool, pending_new: dict[str, str]):
    row = prow.row
    if row.email:
        user = User.objects.filter(email__iexact=row.email).first()
        if user is not None:
            prow.owner, prow.owner_exists = user, True
            return
        if not create_users:
            prow.action = ACTION_ERROR
            prow.messages.append(_("No account for %(e)s - enable 'create missing users'.") % {"e": row.email})
            return
        prow.create_user = True
        if row.email in pending_new:  # same new user on an earlier line of this file
            prow.new_username = pending_new[row.email]
        else:
            wanted = row.username or row.email.split("@")[0]
            taken = {u.lower() for u in pending_new.values()}
            candidate, i = _unique_username(wanted), 1
            while candidate.lower() in taken:
                i += 1
                candidate = _unique_username(f"{wanted}{i}")
            prow.new_username = pending_new[row.email] = candidate
        prow.messages.append(_("Creates user %(u)s.") % {"u": prow.new_username})
        if row.username and prow.new_username.lower() != row.username.lower():
            prow.messages.append(_("Nickname '%(n)s' is taken; '%(u)s' will be used.") % {
                "n": row.username, "u": prow.new_username})
        return
    if row.username:
        user = User.objects.filter(username__iexact=row.username).first()
        if user is None:
            prow.action = ACTION_ERROR
            prow.messages.append(_("No user with nickname '%(n)s' (add an e-mail column to create users).") % {
                "n": row.username})
            return
        prow.owner, prow.owner_exists = user, True
        return
    if not allow_ownerless:
        prow.action = ACTION_ERROR
        prow.messages.append(_("Row has no owner (e-mail or username)."))
        return
    prow.ownerless = True
    prow.messages.append(_("No owner - the number will belong to the event (orga)."))


def preview(event, rows, *, create_users: bool = False, allow_ownerless: bool = True, actor=None) -> ImportPlan:
    """Dry run: decide per row whether it would be created, skipped (number taken) or fails."""
    groups = {g.slug: g for g in UserGroup.objects.filter(event=event)}
    seen_numbers: dict[str, int] = {}
    pending_new: dict[str, str] = {}
    plan = ImportPlan(rows=[], unknown_columns=list(getattr(rows, "unknown_columns", [])))
    for row in rows:
        prow = PlanRow(row=row)
        plan.rows.append(prow)
        if row.errors:
            prow.action = ACTION_ERROR
            prow.messages.extend(row.errors)
            continue
        if row.number in seen_numbers:
            prow.action = ACTION_ERROR
            prow.messages.append(_("Duplicate number in this file (see line %(l)d).") % {
                "l": seen_numbers[row.number]})
            continue
        seen_numbers[row.number] = row.line
        _resolve_owner(prow, create_users=create_users, allow_ownerless=allow_ownerless, pending_new=pending_new)
        if prow.action == ACTION_ERROR:
            continue
        if row.group and row.group not in groups:
            prow.action = ACTION_ERROR
            prow.messages.append(_("Unknown user group '%(g)s'.") % {"g": row.group})
            continue
        if row.group and prow.ownerless:
            prow.messages.append(_("Group is ignored for rows without an owner."))
        if not event.registration_open and not (prow.owner is not None and prow.owner.is_orga(event)):
            prow.action = ACTION_ERROR
            prow.messages.append(_("Registration is not open for this event."))
            continue
        av = services.check_availability(event, row.number, user=prow.owner, extension_type=row.type,
                                         with_suggestions=False)
        if av.taken:
            prow.action = ACTION_SKIP_TAKEN
            prow.messages.append(av.reason)
            continue
        if not av.policy.allowed:
            # Membership/group changes happen before registration, so a "restricted" verdict may flip.
            if av.policy.code == "restricted" and (prow.create_user or row.group or row.role):
                prow.messages.append(_("Restricted range - checked again once the membership is applied."))
            else:
                prow.action = ACTION_ERROR
                prow.messages.append(av.policy.reason)
                continue
        if not av.quota_ok:
            prow.action = ACTION_ERROR
            prow.messages.append(_("Owner has reached the extension quota."))
            continue
        if av.requires_approval:
            prow.messages.append(_("Activated without moderation (orga import)."))
    return plan


# --- apply ------------------------------------------------------------------------

@dataclass
class ImportResult:
    plan: ImportPlan
    created: list = field(default_factory=list)  # Extension instances
    users_created: list = field(default_factory=list)  # User instances
    errors: list[dict] = field(default_factory=list)  # {"line", "number", "message"}
    mails_sent: int = 0

    @property
    def applied(self) -> int:
        return len(self.created)

    @property
    def skipped(self) -> int:
        return self.plan.skipped

    def as_dict(self) -> dict:
        return {"plan": self.plan.as_list(), "applied": self.applied, "users_created": len(self.users_created),
                "skipped": self.skipped, "errors": self.errors}


def _ensure_membership(event, user, role: str):
    membership, created = EventMembership.objects.get_or_create(
        event=event, user=user, defaults={"role": role or EventMembership.Role.USER})
    if not created and role and _ROLE_RANK.get(role, 0) > _ROLE_RANK.get(membership.role, 0):
        membership.role = role
        membership.save(update_fields=["role", "updated_at"])
    return membership


def _apply_row(event, prow: PlanRow, actor, groups: dict, *, request=None):
    """Create owner (if needed), membership, group and the extension. Runs inside one transaction."""
    row = prow.row
    new_user = None
    owner = prow.owner
    if prow.create_user:
        owner = User.objects.filter(email__iexact=row.email).first()  # created by an earlier row?
        if owner is None:
            owner = User.objects.create_user(email=row.email, username=_unique_username(prow.new_username),
                                             password=None)
            new_user = owner
        prow.owner = owner
    if owner is not None:
        membership = _ensure_membership(event, owner, row.role)
        if row.group and row.group in groups:
            membership.groups.add(groups[row.group])
    fields: dict = {"description": row.description, "location_hint": row.location}
    if row.display_name:
        fields["display_name"] = row.display_name
    if row.in_phonebook is not None:
        fields["in_phonebook"] = row.in_phonebook
    ext = services.register(event, owner, row.number, row.type, force_active=True, request=request, **fields)
    return ext, new_user


def apply(event, rows, actor, *, create_users: bool = False, allow_ownerless: bool = True, request=None,
          send_mails: bool = True) -> ImportResult:
    """Import ``rows``. Each row is its own transaction; failures are collected in ``result.errors``."""
    plan = preview(event, rows, create_users=create_users, allow_ownerless=allow_ownerless, actor=actor)
    result = ImportResult(plan=plan)
    groups = {g.slug: g for g in UserGroup.objects.filter(event=event)}
    for prow in plan.rows:
        if prow.action != ACTION_CREATE:
            result.errors.append({"line": prow.row.line, "number": prow.row.number, "action": prow.action,
                                  "message": "; ".join(str(m) for m in prow.messages)})
            continue
        try:
            with transaction.atomic():
                ext, new_user = _apply_row(event, prow, actor, groups, request=request)
        except (services.ExtensionError, IntegrityError, ValidationError, ValueError) as exc:
            prow.action = ACTION_ERROR
            prow.messages.append(str(exc))
            result.errors.append({"line": prow.row.line, "number": prow.row.number, "action": ACTION_ERROR,
                                  "message": str(exc)})
            continue
        prow.action = ACTION_CREATED
        prow.extension_id = str(ext.pk)
        result.created.append(ext)
        if new_user is not None:
            result.users_created.append(new_user)
    if send_mails and result.users_created:
        from apps.accounts.tokens import send_invitation_mail

        for u in result.users_created:
            if send_invitation_mail(u, event=event, request=request):
                result.mails_sent += 1
    log(action="create", actor=actor, target=event, event=event, request=request,
        message=f"CSV import: {result.applied} extensions, {len(result.users_created)} users",
        changes={"extensions": result.applied, "users": len(result.users_created), "skipped": result.skipped,
                 "errors": len(result.errors) - result.skipped})
    return result
