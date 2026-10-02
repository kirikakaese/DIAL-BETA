"""Portal views: public page list / page view plus the orga management pages (mounted at /e/<slug>/pages/)."""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.audit import log as audit
from apps.events.webhooks import emit
from apps.portal.shortcuts import require_orga, with_event

from .forms import InfoPageForm
from .models import InfoPage


def _is_orga(request, event) -> bool:
    return request.user.is_authenticated and request.user.is_orga(event)


def visible_pages(event, user):
    """Published pages for everyone; orga/admin also see unpublished ones."""
    qs = InfoPage.objects.filter(event=event)
    if not (user.is_authenticated and user.is_orga(event)):
        qs = qs.filter(published=True)
    return qs


def page_payload(page: InfoPage) -> dict:
    return {"id": page.pk, "event": page.event.slug, "slug": page.slug, "title": page.title,
            "published": page.published, "show_on_dashboard": page.show_on_dashboard,
            "updated_at": page.updated_at.isoformat()}


def notify(page: InfoPage, action: str, actor, request=None, changes=None):
    """Audit + webhook for a page change (shared with the REST API)."""
    audit(action=action, actor=actor, target=page, event=page.event, request=request,
          message=f"Info page {action}d: {page.title} ({page.slug})", changes=changes)
    if action != "delete":
        emit("page.updated", dict(page_payload(page), action=action), event=page.event)


# --------------------------------------------------------------------------- reading


@with_event
def index(request, slug, *, event):
    return render(request, "pages/index.html", {
        "event": event, "pages": visible_pages(event, request.user), "is_orga": _is_orga(request, event),
    })


@with_event
def show(request, slug, page_slug, *, event):
    page = get_object_or_404(visible_pages(event, request.user), slug=page_slug)
    return render(request, "pages/show.html", {
        "event": event, "page": page, "is_orga": _is_orga(request, event),
        "others": visible_pages(event, request.user).exclude(pk=page.pk),
    })


# --------------------------------------------------------------------------- orga


@require_orga
def manage(request, slug, *, event):
    return render(request, "pages/manage.html", {
        "event": event, "pages": InfoPage.objects.filter(event=event).select_related("updated_by"),
    })


@require_orga
def create(request, slug, *, event):
    form = InfoPageForm(request.POST or None, event=event, instance=InfoPage(event=event))
    if request.method == "POST" and form.is_valid():
        page = form.save(commit=False)
        page.updated_by = request.user
        page.save()
        notify(page, "create", request.user, request)
        messages.success(request, _("Page “%(title)s” created.") % {"title": page.title})
        return redirect("pages:manage", event.slug)
    return render(request, "pages/form.html", {"event": event, "form": form, "page": None})


@require_orga
def edit(request, slug, page_slug, *, event):
    page = get_object_or_404(InfoPage, event=event, slug=page_slug)
    form = InfoPageForm(request.POST or None, event=event, instance=page)
    if request.method == "POST" and form.is_valid():
        changes = {k: [form.initial.get(k), form.cleaned_data[k]] for k in form.changed_data if k != "body"}
        if "body" in form.changed_data:
            changes["body"] = ["…", "…"]
        page = form.save(commit=False)
        page.updated_by = request.user
        page.save()
        notify(page, "update", request.user, request, changes=changes)
        messages.success(request, _("Page “%(title)s” saved.") % {"title": page.title})
        return redirect("pages:manage", event.slug)
    return render(request, "pages/form.html", {"event": event, "form": form, "page": page})


@require_orga
@require_POST
def delete(request, slug, page_slug, *, event):
    page = get_object_or_404(InfoPage, event=event, slug=page_slug)
    notify(page, "delete", request.user, request)
    title = page.title
    page.delete()
    messages.success(request, _("Page “%(title)s” deleted.") % {"title": title})
    return redirect("pages:manage", event.slug)
