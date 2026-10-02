"""Portal views: group overview with my login toggles, create group, group detail/management, admins,
invites (send / cancel / accept / decline) and the "my memberships" page."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.features import require
from apps.extensions.models import Extension
from apps.extensions.services import get_plan
from apps.portal.shortcuts import with_event

from . import services
from .forms import AddMemberForm, AdminForm, CreateGroupForm, GroupSettingsForm, InviteForm, MemberSettingsForm
from .models import CallGroup, CallGroupInvite, GroupMember
from .services import CallGroupError


def _groups(event):
    return (CallGroup.objects.filter(event=event, extension__state=Extension.State.ACTIVE)
            .select_related("extension__owner", "user_group")
            .annotate(total=Count("members", distinct=True),
                      online=Count("members", filter=Q(members__logged_in=True), distinct=True)))


def _group(event, pk):
    return get_object_or_404(CallGroup.objects.select_related("extension__owner", "user_group"), pk=pk, event=event)


def _member(group, mpk):
    return get_object_or_404(GroupMember.objects.select_related("extension__owner", "group__extension"),
                             pk=mpk, group=group)


def _detail_url(group):
    return reverse("callgroups:detail", args=[group.event.slug, group.pk])


def _invite(event, token):
    return get_object_or_404(CallGroupInvite.objects.select_related("group__extension__owner", "extension__owner",
                                                                     "invited_by"),
                             token=token, group__event=event)


@login_required
@with_event
@require("callgroups")
def index(request, slug, *, event):
    return render(request, "callgroups/index.html", {
        "event": event, "groups": _groups(event), "plan": get_plan(event),
        "my_memberships": services.my_memberships(event, request.user),
        "my_groups": [g for g in _groups(event) if g.extension.owner_id == request.user.pk],
        "open_invites": services.open_invites_for(event, request.user),
    })


@login_required
@with_event
@require("callgroups")
def mine(request, slug, *, event):
    """My memberships (toggle / leave) and open invitations for my extensions."""
    return render(request, "callgroups/mine.html", {
        "event": event, "plan": get_plan(event),
        "my_memberships": services.my_memberships(event, request.user),
        "open_invites": services.open_invites_for(event, request.user),
    })


@login_required
@with_event
@require("callgroups")
def create(request, slug, *, event):
    form = CreateGroupForm(request.POST or None, event=event)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            group = services.create_group(
                event, request.user, d["number"], d["name"], d["strategy"], request=request,
                description=d["description"], ring_timeout=d["ring_timeout"], wrap_up_seconds=d["wrap_up_seconds"],
                allow_self_service=d["allow_self_service"], user_group=d["user_group"], in_phonebook=d["in_phonebook"],
                shortcode=d["shortcode"],
            )
        except CallGroupError as exc:
            form.add_error(None, str(exc))
        else:
            if group.extension.is_active:
                messages.success(request, _("Group %(n)s created.") % {"n": group.number})
            else:
                messages.info(request, _("Group %(n)s requested - it becomes active once approved.")
                              % {"n": group.number})
            return redirect(_detail_url(group))
    return render(request, "callgroups/create.html", {"event": event, "form": form})


@login_required
@with_event
@require("callgroups")
def detail(request, slug, pk, *, event):
    group = _group(event, pk)
    manage = services.can_manage(request.user, group)
    settings_form = GroupSettingsForm(instance=group, event=event)
    if request.method == "POST" and manage:
        settings_form = GroupSettingsForm(request.POST, instance=group, event=event)
        if settings_form.is_valid():
            d = settings_form.cleaned_data
            try:
                # ModelForm validation already mutated ``group``; diff against a fresh copy
                services.update_group(CallGroup.objects.get(pk=group.pk), request.user, request=request, **d)
                messages.success(request, _("Group settings saved."))
                return redirect(_detail_url(group))
            except CallGroupError as exc:
                settings_form.add_error(None, str(exc))
    members = group.members.select_related("extension__owner").order_by("priority", "extension__number")
    waves = services.dial_waves(group.extension)
    return render(request, "callgroups/detail.html", {
        "event": event, "group": group, "members": members, "can_manage": manage,
        "is_owner_or_orga": services.is_owner_or_orga(request.user, group),
        "settings_form": settings_form, "add_form": AddMemberForm(), "invite_form": InviteForm(),
        "admin_form": AdminForm(), "admins": group.admins.order_by("username"),
        "open_invites": group.open_invites().select_related("extension__owner", "invited_by"),
        "log": services.GroupLoginLog.objects.filter(member__group=group).select_related("member__extension")[:30],
        "targets": services.dial_targets(group.extension), "plan": get_plan(event),
        "waves": waves if len(waves) > 1 else [],
        "my_extension_ids": set(Extension.objects.filter(event=event, owner=request.user).values_list("pk", flat=True)),
    })


@login_required
@with_event
@require("callgroups")
@require_POST
def add_member(request, slug, pk, *, event):
    group = _group(event, pk)
    if not services.can_manage(request.user, group):
        raise PermissionDenied
    form = AddMemberForm(request.POST)
    if form.is_valid():
        ext = services._active_extension(event, form.cleaned_data["number"])
        if ext is None:
            messages.error(request, _("No active extension with that number."))
        else:
            try:
                services.add_member(group, ext, request.user, request=request,
                                    priority=form.cleaned_data.get("priority") or 0,
                                    delay_s=form.cleaned_data.get("delay_s") or 0)
                messages.success(request, _("%(n)s added to the group.") % {"n": ext.number})
            except CallGroupError as exc:
                messages.error(request, str(exc))
    else:
        messages.error(request, _("Please enter an extension number."))
    return redirect(_detail_url(group))


@login_required
@with_event
@require("callgroups")
@require_POST
def member_settings(request, slug, pk, mpk, *, event):
    group = _group(event, pk)
    member = _member(group, mpk)
    if not services.can_manage(request.user, group):
        raise PermissionDenied
    form = MemberSettingsForm(request.POST)
    if form.is_valid():
        services.update_member(member, request.user, request=request, **form.cleaned_data)
        messages.success(request, _("%(n)s updated.") % {"n": member.number})
    else:
        messages.error(request, _("Invalid priority or delay."))
    return redirect(_detail_url(group))


@login_required
@with_event
@require("callgroups")
@require_POST
def remove_member(request, slug, pk, mpk, *, event):
    group = _group(event, pk)
    member = _member(group, mpk)
    if not (services.can_manage(request.user, group) or member.extension.owner_id == request.user.pk):
        raise PermissionDenied
    services.remove_member(member, request.user, request=request)
    messages.success(request, _("%(n)s removed from the group.") % {"n": member.number})
    return redirect(request.POST.get("next") or _detail_url(group))


@login_required
@with_event
@require("callgroups")
@require_POST
def leave(request, slug, pk, mpk, *, event):
    """Extension owner takes their own extension out of the group."""
    group = _group(event, pk)
    member = _member(group, mpk)
    services.leave(member, request.user, request=request)  # raises PermissionDenied for non-owners
    messages.success(request, _("%(n)s left %(g)s.") % {"n": member.number, "g": group.name})
    return redirect(request.POST.get("next") or reverse("callgroups:mine", args=[event.slug]))


@login_required
@with_event
@require("callgroups")
@require_POST
def toggle(request, slug, pk, mpk, action, *, event):
    group = _group(event, pk)
    member = _member(group, mpk)
    if not services.can_toggle(request.user, member):
        raise PermissionDenied
    if action == "login":
        services.login(member, "web", actor=request.user, request=request)
        messages.success(request, _("%(n)s is now logged in to %(g)s.") % {"n": member.number, "g": group.name})
    else:
        services.logout(member, "web", actor=request.user, request=request)
        messages.success(request, _("%(n)s is now logged out of %(g)s.") % {"n": member.number, "g": group.name})
    return redirect(request.POST.get("next") or _detail_url(group))


@login_required
@with_event
@require("callgroups")
@require_POST
def delete(request, slug, pk, *, event):
    group = _group(event, pk)
    if not services.can_manage(request.user, group):
        raise PermissionDenied
    services.delete_group(group, request.user, request=request)
    messages.success(request, _("Group deleted."))
    return redirect(reverse("callgroups:index", args=[event.slug]))


# --------------------------------------------------------------------------- admins

@login_required
@with_event
@require("callgroups")
@require_POST
def add_admin(request, slug, pk, *, event):
    group = _group(event, pk)
    form = AdminForm(request.POST)
    if form.is_valid():
        target = services.find_user(form.cleaned_data["identifier"])
        try:
            if services.add_admin(group, target, request.user, request=request):
                messages.success(request, _("%(u)s is now a group admin.") % {"u": target.username})
            else:
                messages.info(request, _("%(u)s already is a group admin.") % {"u": target.username})
        except CallGroupError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, _("Please enter an e-mail address or nickname."))
    return redirect(_detail_url(group))


@login_required
@with_event
@require("callgroups")
@require_POST
def remove_admin(request, slug, pk, upk, *, event):
    group = _group(event, pk)
    target = get_object_or_404(group.admins, pk=upk)
    services.remove_admin(group, target, request.user, request=request)
    messages.success(request, _("%(u)s is no longer a group admin.") % {"u": target.username})
    return redirect(_detail_url(group))


# --------------------------------------------------------------------------- invites

@login_required
@with_event
@require("callgroups")
@require_POST
def invite(request, slug, pk, *, event):
    group = _group(event, pk)
    if not services.can_manage(request.user, group):
        raise PermissionDenied
    form = InviteForm(request.POST)
    if form.is_valid():
        ext = services._active_extension(event, form.cleaned_data["number"])
        if ext is None:
            messages.error(request, _("No active extension with that number."))
        else:
            try:
                services.invite(group, ext, request.user, form.cleaned_data["reason"], request=request)
                messages.success(request, _("Invitation sent to the owner of %(n)s.") % {"n": ext.number})
            except CallGroupError as exc:
                messages.error(request, str(exc))
    else:
        messages.error(request, _("Please enter an extension number."))
    return redirect(_detail_url(group))


@login_required
@with_event
@require("callgroups")
@require_POST
def cancel_invite(request, slug, pk, ipk, *, event):
    group = _group(event, pk)
    inv = get_object_or_404(group.invites.select_related("extension"), pk=ipk)
    try:
        services.cancel_invite(inv, request.user, request=request)
        messages.success(request, _("Invitation of %(n)s cancelled.") % {"n": inv.extension.number})
    except CallGroupError as exc:
        messages.error(request, str(exc))
    return redirect(_detail_url(group))


@login_required
@with_event
@require("callgroups")
def invites(request, slug, *, event):
    """Open invitations addressed to my extensions."""
    return render(request, "callgroups/invites.html", {
        "event": event, "open_invites": services.open_invites_for(event, request.user),
    })


@login_required
@with_event
@require("callgroups")
def invite_respond(request, slug, token, *, event):
    """Landing page of the invitation mail link: accept or decline."""
    inv = _invite(event, token)
    if not services.can_respond(request.user, inv):
        raise PermissionDenied
    if request.method == "POST" and inv.is_open:
        accept = request.POST.get("action") == "accept"
        try:
            services.respond(inv, request.user, accept, request=request)
        except CallGroupError as exc:
            messages.error(request, str(exc))
        else:
            if accept:
                messages.success(request, _("%(n)s joined %(g)s.") % {"n": inv.extension.number, "g": inv.group.name})
            else:
                messages.info(request, _("Invitation declined."))
        return redirect(request.POST.get("next") or reverse("callgroups:mine", args=[event.slug]))
    return render(request, "callgroups/invite.html", {"event": event, "invite": inv})
