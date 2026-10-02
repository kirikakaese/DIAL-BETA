"""Portal views: numbering settings, extension pools, claims (orga) and the claim redeem page (user)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.audit import log
from apps.extensions.models import ExtensionType
from apps.extensions.services import ExtensionError, get_plan
from apps.portal.shortcuts import require_orga, with_event

from . import services
from .forms import ClaimCreateForm, ExtensionPoolForm, NumberingSettingsForm
from .models import ExtensionClaim, ExtensionPool


@require_orga
def settings_view(request, slug, *, event):
    plan = get_plan(event)
    # an all-unchecked POST is an empty dict, so bind on method rather than truthiness
    form = NumberingSettingsForm(request.POST if request.method == "POST" else None, instance=plan)
    if request.method == "POST" and form.is_valid():
        if form.changed_data:
            form.save()
            log(action="update", actor=request.user, target=plan, event=event, request=request,
                message="Numbering settings updated",
                changes={f: [form.initial.get(f), form.cleaned_data.get(f)] for f in form.changed_data})
        messages.success(request, _("Numbering settings saved."))
        return redirect("numbering:settings", event.slug)
    return render(request, "numbering/settings.html", {
        "event": event, "plan": plan, "form": form,
        "pool_count": ExtensionPool.objects.filter(event=event, is_active=True).count(),
        "open_claims": ExtensionClaim.objects.open().filter(event=event).count(),
        "reserved": plan.reserved_numbers(),
    })


# --- pools / random numbers ------------------------------------------------------


@login_required
@with_event
def random_number(request, slug, *, event):
    """``{"number": "4711"}`` - a free number the current user could register instantly (``null`` if none)."""
    ext_type = request.GET.get("type") or None
    if ext_type not in ExtensionType.values:
        ext_type = None
    number = services.random_free_number(event, user=request.user, extension_type=ext_type)
    return JsonResponse({"number": number})


@require_orga
def pools(request, slug, *, event):
    form = ExtensionPoolForm(request.POST or None, event=event)
    if request.method == "POST" and form.is_valid():
        pool = form.save(commit=False)
        pool.event = event
        pool.save()
        log(action="create", actor=request.user, target=pool, event=event, request=request,
            message=f"Extension pool {pool.name} created")
        messages.success(request, _("Pool %(n)s created.") % {"n": pool.name})
        return redirect("numbering:pools", event.slug)
    pool_list = list(ExtensionPool.objects.filter(event=event))
    return render(request, "numbering/pools.html", {
        "event": event, "form": form, "pools": pool_list, "plan": get_plan(event),
        "sample": services.all_free_numbers(event, limit=20) if pool_list else [],
    })


@require_orga
@require_POST
def pool_delete(request, slug, pk, *, event):
    pool = get_object_or_404(ExtensionPool, pk=pk, event=event)
    log(action="delete", actor=request.user, event=event, request=request,
        message=f"Extension pool {pool.name} deleted")
    pool.delete()
    messages.success(request, _("Pool deleted."))
    return redirect("numbering:pools", event.slug)


# --- claims -------------------------------------------------------------------------


@require_orga
def claims(request, slug, *, event):
    form = ClaimCreateForm(request.POST or None, event=event)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            claim = services.create_claim(
                event, d["number"], request.user, email=d["email"], type=d["type"],
                valid_until=d["valid_until"], note=d["note"], send_email=d["send_email"], request=request,
            )
        except ExtensionError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Number %(n)s reserved for %(who)s.")
                             % {"n": claim.number, "who": claim.claimant_label})
            return redirect("numbering:claims", event.slug)
    all_claims = ExtensionClaim.objects.filter(event=event).select_related("user", "redeemed_extension", "created_by")
    return render(request, "numbering/claims.html", {
        "event": event, "form": form,
        "open_claims": [c for c in all_claims if c.is_open],
        "redeemed_claims": [c for c in all_claims if c.is_redeemed],
        "expired_claims": [c for c in all_claims if c.is_expired],
    })


@require_orga
@require_POST
def claim_resend(request, slug, pk, *, event):
    claim = get_object_or_404(ExtensionClaim, pk=pk, event=event)
    if not claim.is_open:
        messages.error(request, _("Only open claims can be re-sent."))
    elif services.send_claim_invite(claim):
        log(action="update", actor=request.user, target=claim, event=event, request=request,
            message=f"Claim invite for {claim.number} re-sent")
        messages.success(request, _("Invite sent to %(m)s.") % {"m": claim.invite_email})
    else:
        messages.error(request, _("This claim has no e-mail address."))
    return redirect("numbering:claims", event.slug)


@require_orga
@require_POST
def claim_delete(request, slug, pk, *, event):
    claim = get_object_or_404(ExtensionClaim, pk=pk, event=event)
    services.delete_claim(claim, request.user, request=request)
    messages.success(request, _("Claim deleted."))
    return redirect("numbering:claims", event.slug)


@login_required
def claim_redeem(request, slug, token):
    """Invite landing page: shows the reserved number, POST turns it into the user's extension.

    Resolved by token (not via the event's visibility) so invitees of private events can get in;
    redeeming makes them a member.
    """
    claim = get_object_or_404(ExtensionClaim.objects.select_related("event", "user"), token=token, event__slug=slug)
    event = claim.event
    request.event = event
    if request.method == "POST":
        try:
            ext = services.redeem_claim(token, request.user, request=request)
        except ExtensionError as exc:
            messages.error(request, str(exc))
            return redirect("numbering:claim_redeem", event.slug, token)
        messages.success(request, _("Extension %(n)s is now yours. Add a device to start calling.")
                         % {"n": ext.number})
        return redirect("portal:extension_detail", event.slug, ext.pk)
    return render(request, "numbering/claim_redeem.html", {
        "event": event, "claim": claim, "is_mine": claim.is_for(request.user),
    })
