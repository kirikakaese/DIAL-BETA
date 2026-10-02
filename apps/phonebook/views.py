"""Portal views: searchable/printable phonebook plus exports. Public events need no login."""
from functools import wraps

from django import forms
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.core.audit import log as audit
from apps.core.features import enabled
from apps.devices.qr import qr_png
from apps.events.models import Event
from apps.extensions.models import Extension, ExtensionType
from apps.portal.shortcuts import get_event_or_404, require_orga

from . import remote, services
from .models import PhonebookSettings


def phonebook_access(view):
    """Resolve the event; anonymous users are allowed for public events, otherwise redirect to login."""

    @wraps(view)
    def wrapper(request, slug, *args, **kwargs):
        if not request.user.is_authenticated:
            ev = Event.objects.filter(slug=slug).first()
            if ev is None or not ev.is_public or ev.state == Event.State.DRAFT:
                return redirect_to_login(request.get_full_path())
        event = get_event_or_404(request, slug)
        if not enabled("phonebook", event):
            raise Http404("Feature disabled")
        return view(request, slug, *args, event=event, **kwargs)

    return wrapper


@phonebook_access
def index(request, slug, *, event):
    q = request.GET.get("q", "")
    etype = request.GET.get("type", "")
    if etype not in ExtensionType.values:
        etype = ""
    settings = services.get_settings(event)
    exts = list(services.entries(event, q=q, type=etype or None))
    rows = [(e, settings.category_for(e.number) if settings.categories else "") for e in exts]
    return render(request, "phonebook/index.html", {
        "event": event, "q": q, "type": etype, "types": ExtensionType.choices, "rows": rows,
        "settings": settings, "count": len(exts), "base_dn": services.base_dn(event),
    })


def _export(request, event, fmt):
    q = request.GET.get("q") or None
    exts = services.entries(event, q=q)
    body, ctype, fname = services.export(event, fmt, exts)
    resp = HttpResponse(body, content_type=ctype)
    disposition = "inline" if fmt == "pdf" and not request.GET.get("download") else "attachment"
    resp["Content-Disposition"] = f'{disposition}; filename="{fname}"'
    return resp


@phonebook_access
def pdf(request, slug, *, event):
    return _export(request, event, "pdf")


@phonebook_access
def csv_export(request, slug, *, event):
    return _export(request, event, "csv")


@phonebook_access
def vcf(request, slug, *, event):
    return _export(request, event, "vcf")


@phonebook_access
def ldif(request, slug, *, event):
    return _export(request, event, "ldif")


@phonebook_access
def json_export(request, slug, *, event):
    return JsonResponse({"event": event.slug, "entries": services.as_dicts(event, q=request.GET.get("q"))})


# --------------------------------------------------------------------------- business card (feature #20)

def _card_extension(request, event, number) -> Extension:
    """Active extension ``number``; hidden entries are only served to their owner and orga/helpdesk (else 404)."""
    ext = (Extension.objects.filter(event=event, number=number).active()
           .select_related("owner", "event").first())
    if ext is None:
        raise Http404("No such extension")
    if not ext.in_phonebook:
        user = request.user
        if not (user.is_authenticated and (ext.owner_id == user.pk or user.is_helpdesk(event))):
            raise Http404("No such extension")
    return ext


def _no_store(resp):
    resp["Cache-Control"] = "private, no-store"
    return resp


@phonebook_access
def vcard_one(request, slug, *, event, number):
    ext = _card_extension(request, event, number)
    resp = HttpResponse(services.render_vcard(event, ext), content_type="text/vcard; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{event.slug}-{ext.number}.vcf"'
    return _no_store(resp)


@phonebook_access
def card_qr(request, slug, *, event, number):
    """QR code carrying the (compact) vCard itself, so a phone camera offers "add contact" offline."""
    ext = _card_extension(request, event, number)
    png = qr_png(services.render_vcard(event, ext, compact=True), box_size=5)
    return _no_store(HttpResponse(png, content_type="image/png"))


@phonebook_access
def card_print(request, slug, *, event, number):
    ext = _card_extension(request, event, number)
    return _no_store(render(request, "phonebook/card_print.html", {
        "event": event, "ext": ext, "name": services.entry_name(ext),
    }))


# --------------------------------------------------------------------------- remote directory (feature #9)

@require_GET
def remote_directory(request, slug, token, vendor):
    """Vendor XML phonebook for desk phones / the OMM. The token is the credential; no login, no session.

    Every failure (unknown event, feature off, directory disabled, wrong token, unknown vendor) is a plain
    404 so the URL does not act as an oracle.
    """
    event = Event.objects.filter(slug=slug).first()
    if event is None or vendor not in remote.RENDERERS:
        raise Http404
    settings = services.get_settings(event)
    if not remote.servable(event, settings) or not remote.token_ok(settings, token):
        raise Http404
    body = remote.render_directory(event, vendor, q=remote.search_term(request.GET))
    resp = HttpResponse(body, content_type=remote.CONTENT_TYPE)
    resp["Cache-Control"] = "no-store"
    resp["X-Robots-Tag"] = "noindex"
    return resp


class SettingsForm(forms.ModelForm):
    class Meta:
        model = PhonebookSettings
        fields = ["intro_text", "show_location", "show_owner", "categories", "directory_enabled"]
        widgets = {"intro_text": forms.Textarea(attrs={"rows": 3})}


@require_orga
def settings_view(request, slug, *, event):
    obj = services.get_settings(event)
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(action="update", actor=request.user, target=obj, event=event, request=request,
              message="Phonebook settings changed",
              changes={k: [None, form.cleaned_data[k]] for k in form.changed_data})
        messages.success(request, _("Phonebook settings saved."))
        return redirect(reverse("phonebook:index", args=[event.slug]))
    directory = remote.directory_info(event, obj)
    return render(request, "phonebook/settings.html", {
        "event": event, "form": form, "directory": directory,
        "vendor_urls": [(v, remote.VENDOR_LABELS[v], directory["urls"][v]) for v in remote.VENDORS],
    })


@require_orga
@require_POST
def rotate_directory_token(request, slug, *, event):
    """New directory token: every phone/OMM/LDAP client configured with the old one is locked out."""
    obj = services.get_settings(event)
    obj.rotate_directory_token()
    audit(action="update", actor=request.user, target=obj, event=event, request=request,
          message="Phonebook directory token rotated", changes={"directory_token": ["***", "***"]})
    messages.success(request, _("Directory token rotated. Update the phones and the OMM with the new URLs."))
    return redirect(reverse("phonebook:settings", args=[event.slug]))
