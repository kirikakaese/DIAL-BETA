"""Call group business logic: creation, membership, admins, invites, login/logout (portal, API, feature
codes), and the routing helpers ``dial_targets`` / ``dial_waves`` used by ``GET /api/v1/pbx/route/``."""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.audit import log
from apps.core.features import enabled
from apps.events.webhooks import emit
from apps.extensions.models import ENDPOINT_TYPES, Extension, ExtensionType
from apps.extensions.services import ExtensionError, get_plan, register
from apps.extensions.services import update as update_extension

from .models import CallGroup, CallGroupInvite, GroupLoginLog, GroupMember

#: How deep nested groups are expanded when resolving dial targets: root -> sub -> sub-sub -> sub-sub-sub.
MAX_NESTING_DEPTH = 3
#: Static dialplan context that hosts the delayed-leg exten ``_XXX*X.`` (see ``[pet-group]``).
DELAYED_LEG_CONTEXT = "pet-group"


class CallGroupError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def _active_extension(event, number: str) -> Extension | None:
    number = (number or "").strip()
    if not number:
        return None
    return Extension.objects.filter(event=event, number=number).active().select_related("owner").first()


def group_for_number(event, number: str) -> CallGroup | None:
    return (CallGroup.objects.filter(event=event, extension__number=(number or "").strip())
            .select_related("extension").first())


def nested_group(extension: Extension | None) -> CallGroup | None:
    """The :class:`CallGroup` behind a ``group``-type extension (``None`` for endpoints)."""
    if extension is None or extension.type != ExtensionType.GROUP:
        return None
    return CallGroup.objects.filter(extension=extension).select_related("extension").first()


def find_user(identifier: str):
    """Look a user up by e-mail or nickname (username), case-insensitively."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    user_model = get_user_model()
    return (user_model.objects.filter(email__iexact=ident).first()
            or user_model.objects.filter(username__iexact=ident).first())


def is_owner_or_orga(user, group: CallGroup) -> bool:
    if user is None or not user.is_authenticated:
        return False
    return group.extension.owner_id == user.pk or user.is_orga(group.event)


def can_manage(user, group: CallGroup) -> bool:
    """Group owner (extension owner), a group admin or event orga may edit the group and its member list."""
    if is_owner_or_orga(user, group):
        return True
    return group.admins.filter(pk=user.pk).exists()


def can_toggle(user, member: GroupMember) -> bool:
    """Who may log a member in/out: the member's extension owner (if self-service is on) or a manager."""
    if can_manage(user, member.group):
        return True
    return member.group.allow_self_service and member.extension.owner_id == user.pk


def can_respond(user, invite: CallGroupInvite) -> bool:
    """Only the invited extension's owner (or orga) may accept/decline."""
    if user is None or not user.is_authenticated:
        return False
    return invite.extension.owner_id == user.pk or user.is_orga(invite.group.event)


def _sync_extension_config(group: CallGroup) -> None:
    """Mirror strategy/members into ``Extension.config`` so the PBX route degrades gracefully."""
    ext = group.extension
    cfg = dict(ext.config or {})
    cfg["strategy"] = "roundrobin" if group.is_serial else "parallel"
    cfg["members"] = [m.extension.number for m in group.members.filter(logged_in=True).select_related("extension")]
    cfg["callgroup_id"] = group.pk
    changes = {}
    if cfg != (ext.config or {}):
        changes["config"] = cfg
    wanted = Extension.RingStrategy.SERIAL if group.is_serial else Extension.RingStrategy.PARALLEL
    if ext.ring_strategy != wanted:
        changes["ring_strategy"] = wanted
    if ext.ring_timeout != group.ring_timeout:
        changes["ring_timeout"] = group.ring_timeout
    if changes:
        update_extension(ext, None, **changes)


# --------------------------------------------------------------------------- lifecycle

@transaction.atomic
def create_group(event, owner, number: str, name: str, strategy: str = CallGroup.Strategy.RING_ALL, *,
                 request=None, description: str = "", ring_timeout: int = 20, wrap_up_seconds: int = 0,
                 allow_self_service: bool = True, user_group=None, in_phonebook: bool = True,
                 shortcode: str = "") -> CallGroup:
    """Register the ``group`` extension for ``owner`` and wrap it in a :class:`CallGroup`."""
    if not enabled("callgroups", event):
        raise CallGroupError(_("Call groups are disabled for this event."))
    if strategy not in CallGroup.Strategy.values:
        raise CallGroupError(_("Unknown ring strategy."))
    try:
        ext = register(event, owner, number, ExtensionType.GROUP, request=request,
                       display_name=(name or "")[:60], description=description[:200], in_phonebook=in_phonebook)
    except ExtensionError as exc:
        raise CallGroupError(str(exc)) from exc
    group = CallGroup.objects.create(
        event=event, extension=ext, strategy=strategy, description=description, ring_timeout=ring_timeout,
        wrap_up_seconds=wrap_up_seconds, allow_self_service=allow_self_service, user_group=user_group,
        shortcode=(shortcode or "").strip()[:8],
    )
    log(action="create", actor=owner, target=group, event=event, request=request,
        message=f"Call group {number} created", changes={"strategy": [None, strategy]})
    if user_group is not None:
        sync_user_group(group, actor=owner)
    _sync_extension_config(group)
    return group


def update_group(group: CallGroup, actor, request=None, **fields) -> CallGroup:
    changes = {}
    name = fields.pop("name", None)
    if "shortcode" in fields:
        fields["shortcode"] = (fields["shortcode"] or "").strip()[:8]
    for k, v in fields.items():
        old = getattr(group, k)
        if old != v:
            changes[k] = [str(old) if old is not None else None, str(v) if v is not None else None]
            setattr(group, k, v)
    if "strategy" in changes and group.strategy not in CallGroup.Strategy.values:
        raise CallGroupError(_("Unknown ring strategy."))
    if changes:
        group.save()
        log(action="update", actor=actor, target=group, event=group.event, request=request, changes=changes)
    if name is not None and name != group.extension.display_name:
        update_extension(group.extension, actor, request=request, display_name=name[:60])
    if "user_group" in changes and group.user_group_id:
        sync_user_group(group, actor=actor)
    _sync_extension_config(group)
    return group


def delete_group(group: CallGroup, actor, request=None) -> None:
    from apps.extensions.services import delete as delete_extension

    ext = group.extension
    log(action="delete", actor=actor, target=group, event=group.event, request=request,
        message=f"Call group {group.number} deleted")
    group.delete()
    if ext.state in (Extension.State.ACTIVE, Extension.State.REQUESTED, Extension.State.SUSPENDED):
        delete_extension(ext, actor, request=request)


# --------------------------------------------------------------------------- admins

def add_admin(group: CallGroup, user, actor, *, request=None) -> bool:
    """Grant ``user`` management rights on ``group`` (owner/orga only). Returns ``True`` when newly added."""
    if not is_owner_or_orga(actor, group):
        raise PermissionDenied(_("Only the group owner or orga may change group admins."))
    if user is None:
        raise CallGroupError(_("No user with that e-mail or nickname."))
    if user.pk == group.extension.owner_id:
        raise CallGroupError(_("%(u)s already owns this group.") % {"u": user.username})
    if group.admins.filter(pk=user.pk).exists():
        return False
    group.admins.add(user)
    log(action="update", actor=actor, target=group, event=group.event, request=request,
        message=f"Group admin {user.username} added", changes={"admin": [None, user.username]})
    return True


def remove_admin(group: CallGroup, user, actor, *, request=None) -> bool:
    if not is_owner_or_orga(actor, group):
        raise PermissionDenied(_("Only the group owner or orga may change group admins."))
    if user is None or not group.admins.filter(pk=user.pk).exists():
        return False
    group.admins.remove(user)
    log(action="update", actor=actor, target=group, event=group.event, request=request,
        message=f"Group admin {user.username} removed", changes={"admin": [user.username, None]})
    return True


# --------------------------------------------------------------------------- members

def _contains_group(group: CallGroup, needle_id: int, depth: int = 0, visited: set | None = None) -> bool:
    """``True`` when ``needle_id`` is reachable from ``group`` through nested memberships (any depth)."""
    visited = visited if visited is not None else set()
    if group.pk in visited:
        return False
    visited.add(group.pk)
    subs = CallGroup.objects.filter(extension__group_memberships__group=group).select_related("extension")
    for sub in subs:
        if sub.pk == needle_id or _contains_group(sub, needle_id, depth + 1, visited):
            return True
    return False


@transaction.atomic
def add_member(group: CallGroup, extension: Extension, actor=None, *, via: str = "web", request=None,
               priority: int = 0, logged_in: bool = True, delay_s: int = 0) -> GroupMember:
    if extension.event_id != group.event_id:
        raise CallGroupError(_("Extension does not belong to this event."))
    if not extension.is_active:
        raise CallGroupError(_("Only active extensions can join a group."))
    if extension.pk == group.extension_id:
        raise CallGroupError(_("A group cannot be a member of itself."))
    if extension.type == ExtensionType.GROUP:
        sub = nested_group(extension)
        if sub is None:
            raise CallGroupError(_("%(n)s is not a managed call group.") % {"n": extension.number})
        if _contains_group(sub, group.pk):
            raise CallGroupError(_("Adding %(n)s would create a loop of nested groups.") % {"n": extension.number})
    elif extension.type not in ENDPOINT_TYPES:
        raise CallGroupError(_("Only endpoint extensions (handsets, SIP phones...) or other call groups "
                               "can be group members."))
    member, created = GroupMember.objects.get_or_create(
        group=group, extension=extension,
        defaults={"added_by": actor, "priority": priority, "logged_in": logged_in, "delay_s": delay_s})
    if created:
        GroupLoginLog.objects.create(member=member, action=GroupLoginLog.Action.ADDED, via=via)
        log(action="update", actor=actor, target=group, event=group.event, request=request,
            message=f"Member {extension.number} added", changes={"member": [None, extension.number]})
        _sync_extension_config(group)
    return member


def update_member(member: GroupMember, actor=None, *, request=None, **fields) -> GroupMember:
    """Change ``priority`` / ``delay_s`` of a membership."""
    changes = {}
    for k in ("priority", "delay_s"):
        if k in fields and fields[k] is not None and getattr(member, k) != fields[k]:
            changes[k] = [getattr(member, k), fields[k]]
            setattr(member, k, fields[k])
    if changes:
        member.save(update_fields=[*changes, "updated_at"])
        log(action="update", actor=actor, target=member.group, event=member.group.event, request=request,
            message=f"Member {member.extension.number} updated", changes=changes)
    return member


@transaction.atomic
def remove_member(member: GroupMember, actor=None, *, via: str = "web", request=None) -> None:
    group = member.group
    number = member.extension.number
    log(action="update", actor=actor, target=group, event=group.event, request=request,
        message=f"Member {number} removed", changes={"member": [number, None]})
    member.delete()
    _sync_extension_config(group)


def leave(member: GroupMember, user, *, via: str = "web", request=None) -> None:
    """The owner of a member extension takes it out of the group (managers use :func:`remove_member`)."""
    if user is None or not user.is_authenticated or member.extension.owner_id != user.pk:
        raise PermissionDenied(_("Only the owner of the extension may leave the group with it."))
    remove_member(member, user, via=via, request=request)


def _set_logged_in(member: GroupMember, state: bool, via: str, actor=None, request=None) -> GroupMember:
    if member.logged_in != state:
        member.logged_in = state
        member.save(update_fields=["logged_in", "updated_at"])
        action = GroupLoginLog.Action.LOGIN if state else GroupLoginLog.Action.LOGOUT
        GroupLoginLog.objects.create(member=member, action=action, via=via)
        log(action="update", actor=actor, target=member.group, event=member.group.event, request=request,
            message=f"{member.extension.number} {'logged in' if state else 'logged out'} via {via}",
            changes={"logged_in": [not state, state]})
        _sync_extension_config(member.group)
    return member


def login(member: GroupMember, via: str = "web", *, actor=None, request=None) -> GroupMember:
    return _set_logged_in(member, True, via, actor=actor, request=request)


def logout(member: GroupMember, via: str = "web", *, actor=None, request=None) -> GroupMember:
    return _set_logged_in(member, False, via, actor=actor, request=request)


def sync_user_group(group: CallGroup, actor=None) -> int:
    """Auto-join active endpoint extensions of every member of ``group.user_group``. Returns #added."""
    if group.user_group_id is None:
        return 0
    from apps.events.models import EventMembership

    users = EventMembership.objects.filter(event=group.event, groups=group.user_group).values_list("user_id", flat=True)
    exts = (Extension.objects.filter(event=group.event, owner_id__in=list(users), type__in=ENDPOINT_TYPES)
            .active().exclude(group_memberships__group=group))
    n = 0
    for ext in exts:
        add_member(group, ext, actor, via="auto")
        n += 1
    return n


# --------------------------------------------------------------------------- invites

def _invite_url(invite: CallGroupInvite) -> str:
    base = (getattr(settings, "PET_PUBLIC_URL", "") or "").rstrip("/")
    return f"{base}/e/{invite.group.event.slug}/callgroups/invites/{invite.token}/"


def _send_invite_mail(invite: CallGroupInvite) -> bool:
    owner = invite.extension.owner
    if owner is None or not owner.email:
        return False
    group = invite.group
    who = invite.invited_by.username if invite.invited_by else "the group owner"
    body = (
        f"{who} invites your extension {invite.extension.number} to join the call group "
        f"{group.name} ({group.number}) at {group.event.name}.\n"
    )
    if invite.reason:
        body += f"\nReason: {invite.reason}\n"
    body += f"\nAccept or decline here:\n{_invite_url(invite)}\n"
    send_mail(f"[PET] Invitation to call group {group.number} ({group.name})", body,
              settings.DEFAULT_FROM_EMAIL, [owner.email], fail_silently=True)
    return True


def _invite_payload(invite: CallGroupInvite) -> dict:
    return {
        "event": invite.group.event.slug, "group": invite.group.number, "group_id": invite.group_id,
        "extension": invite.extension.number, "invite_id": invite.pk, "status": invite.status,
        "invited_by": invite.invited_by.username if invite.invited_by else None, "reason": invite.reason,
    }


@transaction.atomic
def invite(group: CallGroup, extension: Extension, actor, reason: str = "", *, request=None) -> CallGroupInvite:
    """Ask the owner of ``extension`` to join ``group``; they accept/decline via mail link or portal."""
    if not can_manage(actor, group):
        raise PermissionDenied(_("Only group managers may invite extensions."))
    if extension.event_id != group.event_id:
        raise CallGroupError(_("Extension does not belong to this event."))
    if not extension.is_active or extension.type not in ENDPOINT_TYPES:
        raise CallGroupError(_("Only active endpoint extensions (handsets, SIP phones...) can be invited."))
    if group.members.filter(extension=extension).exists():
        raise CallGroupError(_("%(n)s is already a member of this group.") % {"n": extension.number})
    if group.open_invites().filter(extension=extension).exists():
        raise CallGroupError(_("%(n)s already has an open invitation.") % {"n": extension.number})
    inv = CallGroupInvite.objects.create(group=group, extension=extension, invited_by=actor,
                                         reason=(reason or "").strip()[:200])
    _send_invite_mail(inv)
    log(action="update", actor=actor, target=group, event=group.event, request=request,
        message=f"{extension.number} invited to group {group.number}", changes={"invite": [None, extension.number]})
    emit("callgroup.invited", _invite_payload(inv), event=group.event)
    return inv


@transaction.atomic
def respond(invite: CallGroupInvite, user, accept: bool, *, request=None) -> CallGroupInvite:
    """Extension owner (or orga) accepts -> membership is created; declines -> invite is closed."""
    if not can_respond(user, invite):
        raise PermissionDenied(_("Only the owner of the invited extension may answer this invitation."))
    if not invite.is_open:
        raise CallGroupError(_("This invitation has already been answered."))
    invite.accepted = bool(accept)
    invite.responded_at = timezone.now()
    invite.save(update_fields=["accepted", "responded_at"])
    verb = "accepted" if accept else "declined"
    log(action="update", actor=user, target=invite.group, event=invite.group.event, request=request,
        message=f"{invite.extension.number} {verb} the invitation to group {invite.group.number}",
        changes={"invite": ["open", verb]})
    if accept:
        add_member(invite.group, invite.extension, user, via="invite", request=request)
    emit(f"callgroup.invite_{verb}", _invite_payload(invite), event=invite.group.event)
    return invite


def cancel_invite(invite: CallGroupInvite, actor, *, request=None) -> CallGroupInvite:
    if not can_manage(actor, invite.group):
        raise PermissionDenied(_("Only group managers may cancel invitations."))
    if not invite.is_open:
        raise CallGroupError(_("This invitation has already been answered."))
    invite.responded_at = timezone.now()
    invite.accepted = None
    invite.save(update_fields=["accepted", "responded_at"])
    log(action="update", actor=actor, target=invite.group, event=invite.group.event, request=request,
        message=f"Invitation of {invite.extension.number} to group {invite.group.number} cancelled",
        changes={"invite": ["open", "cancelled"]})
    return invite


def open_invites_for(event, user):
    """Open invitations addressed to any of ``user``'s extensions in ``event``."""
    return (CallGroupInvite.objects.filter(group__event=event, extension__owner=user, responded_at__isnull=True)
            .select_related("group__extension", "extension", "invited_by").order_by("-created_at"))


# --------------------------------------------------------------------------- feature codes

def handle_feature_code(event, caller: str, code: str, target: str) -> bool:
    """``*71 <group>`` logs the caller's extension into the group, ``*72 <group>`` out.

    Without a target every group the caller is a member of is toggled. Returns ``True`` when handled.
    """
    if not enabled("callgroups", event):
        return False
    plan = get_plan(event)
    code = (code or "").strip()
    if code == plan.group_login_code or code in ("group-login", "login"):
        state = True
    elif code == plan.group_logout_code or code in ("group-logout", "logout"):
        state = False
    else:
        return False
    caller_ext = _active_extension(event, caller)
    if caller_ext is None:
        return False
    qs = GroupMember.objects.filter(extension=caller_ext, group__event=event).select_related("group__extension")
    target = (target or "").strip()
    if target:
        qs = qs.filter(group__extension__number=target)
    members = [m for m in qs if m.group.allow_self_service]
    if not members:
        return False
    for m in members:
        _set_logged_in(m, state, "feature-code")
    return True


# --------------------------------------------------------------------------- routing

def _ordered_members(group: CallGroup) -> list[GroupMember]:
    now = timezone.now()
    members = list(group.members.filter(logged_in=True, extension__state=Extension.State.ACTIVE)
                   .select_related("extension"))
    if group.wrap_up_seconds:
        cutoff = now - dt.timedelta(seconds=group.wrap_up_seconds)
        fresh = [m for m in members if not (m.last_call_at and m.last_call_at > cutoff)]
        members = fresh or members  # never leave a group unreachable because everybody is wrapping up
    epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
    if group.strategy == CallGroup.Strategy.LONGEST_IDLE:
        members.sort(key=lambda m: (m.priority, m.last_call_at or epoch, m.extension.number))
    elif group.strategy == CallGroup.Strategy.ROUND_ROBIN:
        members.sort(key=lambda m: (m.priority, m.extension.number))
        # rotate: the member that took the most recent call moves to the end, everyone after them first
        if members:
            last_idx = max(range(len(members)), key=lambda i: members[i].last_call_at or epoch)
            if members[last_idx].last_call_at is not None:
                members = members[last_idx + 1:] + members[:last_idx + 1]
    else:
        members.sort(key=lambda m: (m.priority, m.extension.number))
    return members


def dial_targets(extension: Extension) -> list[str]:
    """Numbers of the logged-in members of the group behind ``extension``, ordered per strategy.

    Nested groups are flattened into their endpoint numbers (depth <= :data:`MAX_NESTING_DEPTH`). In ring-all
    mode members with a ring delay come after the undelayed ones (same order as :func:`dial_waves`).

    Returns ``[]`` when the feature is off or ``extension`` is not a call group (the PBX route then
    falls back to ``extension.config["members"]``).
    """
    return [n for _delay, n in _resolved(extension)]


def dial_waves(extension: Extension) -> list[dict]:
    """Ring-all schedule: ``[{"delay": 0, "targets": [...]}, {"delay": 5, "targets": [...]}]`` by delay.

    Serial strategies ignore per-member delays and yield a single ``delay: 0`` wave in ring order.
    Same fallbacks as :func:`dial_targets` (``[]`` when off / not a group / nobody logged in).
    """
    waves: dict[int, list[str]] = {}
    for delay, number in _resolved(extension):
        waves.setdefault(delay, []).append(number)
    return [{"delay": d, "targets": waves[d]} for d in sorted(waves)]


def delayed_dial_target(number: str, delay: int) -> str:
    """Dial string for a member that must start ringing ``delay`` seconds late.

    ``Local/005*4300@pet-group`` hits the ``_XXX*X.`` exten of the static ``[pet-group]`` context, which
    ``Wait()``s and then dials the number in the event context - Asterisk cannot stagger legs of one
    ``Dial()`` natively.
    """
    return f"Local/{int(delay):03d}*{number}@{DELAYED_LEG_CONTEXT}"


def callerid_prefix(extension: Extension) -> str:
    """``"[SEC] "`` when the group behind ``extension`` has a shortcode, else ``""``."""
    group = nested_group(extension)
    return group.callerid_prefix if group is not None else ""


def _resolved(extension: Extension | None) -> list[tuple[int, str]]:
    """``[(delay_s, number)]`` for the group behind ``extension``: nested groups expanded, duplicates
    collapsed onto their smallest delay, stable-sorted by delay."""
    if extension is None or not enabled("callgroups", extension.event):
        return []
    group = nested_group(extension)
    if group is None:
        return []
    best: dict[str, int] = {}
    order: list[str] = []
    for delay, number in _expand(group, 0, 0, set()):
        if number not in best:
            order.append(number)
            best[number] = delay
        elif delay < best[number]:
            best[number] = delay
    return sorted(((best[n], n) for n in order), key=lambda t: t[0])


def _expand(group: CallGroup, offset: int, depth: int, visited: set[int]) -> list[tuple[int, str]]:
    """Depth-first expansion of ``group``; ``visited`` holds the group ids on the current path."""
    visited = visited | {group.pk}
    out: list[tuple[int, str]] = []
    for m in _ordered_members(group):
        delay = offset + (0 if group.is_serial else m.delay_s)
        if m.is_group:
            sub = nested_group(m.extension)
            if sub is None or sub.pk in visited or depth + 1 > MAX_NESTING_DEPTH:
                continue  # cycle or too deep: the sub-group is silently skipped
            out += _expand(sub, delay, depth + 1, visited)
        else:
            out.append((delay, m.extension.number))
    return out


def record_call(group: CallGroup, member_number: str, at=None) -> GroupMember | None:
    """Note that ``member_number`` answered a call for ``group`` (drives round-robin / longest-idle)."""
    member = (group.members.filter(extension__number=(member_number or "").strip())
              .select_related("extension").first())
    if member is None:
        return None
    member.last_call_at = at or timezone.now()
    member.save(update_fields=["last_call_at", "updated_at"])
    return member


def record_call_from_cdr(event, dst_number: str, answered_number: str | None, at=None) -> GroupMember | None:
    """Hook for the CDR pipeline: ``dst`` was a group and ``answered_number`` picked up."""
    if not answered_number:
        return None
    group = group_for_number(event, dst_number)
    if group is None:
        return None
    return record_call(group, answered_number, at=at)


def my_memberships(event, user):
    return (GroupMember.objects.filter(group__event=event, extension__owner=user)
            .select_related("group__extension", "extension").order_by("group__extension__number"))
