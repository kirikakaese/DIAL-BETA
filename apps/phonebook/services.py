"""Phonebook queries and exporters (HTML data, PDF, CSV, vCard, LDIF, JSON)."""
from __future__ import annotations

import base64
import csv
import io
import re

from django.conf import settings as dj_settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.extensions.models import Extension, ExtensionType

from .models import PhonebookSettings

# Non-endpoint types that are useful to everybody and therefore listed unless explicitly hidden
SERVICE_TYPES = (ExtensionType.GROUP, ExtensionType.ANNOUNCEMENT, ExtensionType.CONFERENCE,
                 ExtensionType.IVR, ExtensionType.APP)


def get_settings(event) -> PhonebookSettings:
    obj, _created = PhonebookSettings.objects.get_or_create(event=event)
    return obj


def base_dn(event) -> str:
    """LDAP base DN used by the LDIF export: ``ou=phonebook,dc=<slug>,dc=dial``."""
    return f"ou=phonebook,dc={event.slug},dc=dial"


def entries(event, q: str | None = None, type: str | None = None):
    """Active extensions listed in the phonebook, ordered by number.

    Endpoint extensions are listed when ``in_phonebook`` is set; groups, announcements, conferences,
    IVRs and apps are listed unless the owner opted out (same flag).
    """
    qs = (Extension.objects.filter(event=event).active().filter(in_phonebook=True)
          .select_related("owner", "event").order_by("number"))
    if type:
        qs = qs.filter(type=type)
    q = (q or "").strip()
    if q:
        qs = qs.filter(
            Q(number__icontains=q) | Q(display_name__icontains=q) | Q(owner__username__icontains=q)
            | Q(description__icontains=q) | Q(location_hint__icontains=q)
        )
    return qs


def entry_name(ext: Extension) -> str:
    return ext.display_name or (ext.owner.username if ext.owner else ext.number)


def card_urls(ext: Extension) -> dict:
    """Absolute ``vcard_url`` / ``card_qr_url`` (business card, feature #20) rooted at ``DIAL_PUBLIC_URL``."""
    base = (getattr(dj_settings, "DIAL_PUBLIC_URL", "") or "").rstrip("/")
    slug = ext.event.slug
    return {
        "vcard_url": base + reverse("phonebook:vcard_one", args=[slug, ext.number]),
        "card_qr_url": base + reverse("phonebook:card_qr", args=[slug, ext.number]),
    }


def as_dict(ext: Extension, settings: PhonebookSettings | None = None) -> dict:
    d = {
        "number": ext.number,
        # ``4700–4799`` for trunk blocks, else the number itself (what lists print)
        "number_label": ext.number_label,
        "name": entry_name(ext),
        "type": ext.type,
        "type_display": str(ext.get_type_display()),
        "description": ext.description,
        **card_urls(ext),
    }
    if settings is None or settings.show_location:
        d["location"] = ext.location_hint
    if settings is None or settings.show_owner:
        d["owner"] = ext.owner.username if ext.owner else None
    if settings is not None and settings.categories:
        d["category"] = settings.category_for(ext.number)
    return d


def as_dicts(event, exts=None, q=None, type=None) -> list[dict]:
    settings = get_settings(event)
    exts = entries(event, q=q, type=type) if exts is None else exts
    return [as_dict(e, settings) for e in exts]


# --------------------------------------------------------------------------- exporters

def render_csv(event, exts=None) -> bytes:
    settings = get_settings(event)
    exts = entries(event) if exts is None else exts
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["number", "name", "type", "owner", "location", "description"])
    for e in exts:
        w.writerow([e.number, entry_name(e), e.type,
                    (e.owner.username if e.owner and settings.show_owner else ""),
                    e.location_hint if settings.show_location else "", e.description])
    return buf.getvalue().encode("utf-8")


def _vcf_escape(v: str) -> str:
    return str(v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _vcard_lines(event, e: Extension, settings: PhonebookSettings, *, compact: bool = False) -> list[str]:
    """vCard 3.0 lines for one entry. ``compact`` keeps only FN/N/TEL/ORG/NOTE so the card fits a small QR code."""
    name = entry_name(e)
    card = [
        "BEGIN:VCARD", "VERSION:3.0",
        f"FN:{_vcf_escape(name)}",
        f"N:{_vcf_escape(name)};;;;",
        f"TEL;TYPE=WORK,VOICE:{e.number}",
        f"ORG:{_vcf_escape(event.name)}",
    ]
    if not compact:
        card += [
            f"CATEGORIES:{_vcf_escape(e.get_type_display())}",
            f"UID:dial-{event.slug}-{e.number}",
            f"X-DIAL-EVENT:{event.slug}",
        ]
    notes = []
    if settings.show_location and e.location_hint:
        notes.append(e.location_hint)
    if e.description:
        notes.append(e.description)
    if notes:
        card.append(f"NOTE:{_vcf_escape(' - '.join(notes))}")
    card.append("END:VCARD")
    return card


def render_vcard(event, ext: Extension, *, compact: bool = False) -> str:
    """A single vCard 3.0 (text, CRLF line ends) - the business card of one extension."""
    return "\r\n".join(_vcard_lines(event, ext, get_settings(event), compact=compact)) + "\r\n"


def render_vcf(event, exts=None) -> bytes:
    """vCard 3.0, one card per entry (importable into most phones / softphones)."""
    settings = get_settings(event)
    exts = entries(event) if exts is None else exts
    out = ["\r\n".join(_vcard_lines(event, e, settings)) for e in exts]
    return ("\r\n".join(out) + "\r\n").encode("utf-8")


_LDIF_SAFE = re.compile(r"^[\x01-\x09\x0b-\x0c\x0e-\x7f]*$")


def _ldif_attr(name: str, value) -> str:
    value = str(value or "")
    if not value or value[0] in " :<" or not _LDIF_SAFE.match(value):
        return f"{name}:: {base64.b64encode(value.encode('utf-8')).decode('ascii')}"
    return f"{name}: {value}"


def render_ldif(event, exts=None) -> bytes:
    """LDIF for hardphone LDAP directories (Snom/Yealink/Gigaset ``inetOrgPerson`` lookups).

    Import into an LDAP server (e.g. ``slapadd`` / ``ldapadd``) below ``dc=<slug>,dc=dial``; configure the
    phones with base DN :func:`base_dn` and a name filter on ``cn``/``sn``, number attribute
    ``telephoneNumber``.
    """
    settings = get_settings(event)
    exts = entries(event) if exts is None else exts
    dn = base_dn(event)
    blocks = [
        "\n".join([f"dn: {dn}", "objectClass: top", "objectClass: organizationalUnit", "ou: phonebook",
                   _ldif_attr("description", f"{event.name} phonebook")]),
    ]
    for e in exts:
        name = entry_name(e)
        parts = name.split(" ", 1)
        given, sn = (parts[0], parts[1]) if len(parts) == 2 else ("", name)
        lines = [
            f"dn: telephoneNumber={e.number},{dn}",
            "objectClass: top", "objectClass: person", "objectClass: organizationalPerson",
            "objectClass: inetOrgPerson",
            _ldif_attr("cn", name), _ldif_attr("sn", sn or name),
            f"telephoneNumber: {e.number}",
            f"uid: {e.number}",
            _ldif_attr("o", event.name),
            _ldif_attr("title", e.get_type_display()),
        ]
        if given:
            lines.append(_ldif_attr("givenName", given))
        if settings.show_location and e.location_hint:
            lines.append(_ldif_attr("physicalDeliveryOfficeName", e.location_hint))
        if e.description:
            lines.append(_ldif_attr("description", e.description))
        if settings.show_owner and e.owner:
            lines.append(_ldif_attr("displayName", e.owner.username))
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n").encode("utf-8")


def render_pdf(event, exts=None) -> bytes:
    """Classic printed phonebook: A4, three columns, event header, page numbers."""
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer

    settings = get_settings(event)
    exts = list(entries(event) if exts is None else exts)
    styles = getSampleStyleSheet()
    entry_style = ParagraphStyle("entry", parent=styles["Normal"], fontName="Helvetica", fontSize=8.5,
                                 leading=10.5, spaceAfter=1.5)
    cat_style = ParagraphStyle("cat", parent=styles["Heading4"], fontName="Helvetica-Bold", fontSize=9.5,
                               leading=12, spaceBefore=4, spaceAfter=2, textColor=colors.HexColor("#333333"))
    intro_style = ParagraphStyle("intro", parent=styles["Normal"], fontSize=8.5, leading=10.5, spaceAfter=4)
    title_style = ParagraphStyle("title", parent=styles["Title"], alignment=TA_CENTER)

    page_w, page_h = A4
    margin = 14 * mm
    top = 22 * mm
    bottom = 16 * mm
    gutter = 6 * mm
    ncols = 3
    col_w = (page_w - 2 * margin - (ncols - 1) * gutter) / ncols
    frames = [Frame(margin + i * (col_w + gutter), bottom, col_w, page_h - top - bottom,
                    leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id=f"col{i}")
              for i in range(ncols)]
    generated = timezone.localtime().strftime("%Y-%m-%d %H:%M")
    title = _("%(event)s - Phonebook") % {"event": event.name}

    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawString(margin, page_h - 14 * mm, title)
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(page_w - margin, page_h - 14 * mm, generated)
        canvas.setStrokeColor(colors.grey)
        canvas.line(margin, page_h - 16 * mm, page_w - margin, page_h - 16 * mm)
        canvas.drawCentredString(page_w / 2, 9 * mm,
                                 _("Page %(n)d") % {"n": doc.page} + f" · {len(exts)} " + _("entries"))
        canvas.restoreState()

    doc = BaseDocTemplate(io.BytesIO(), pagesize=A4, title=title, author="DIAL",
                          leftMargin=margin, rightMargin=margin, topMargin=top, bottomMargin=bottom)
    doc.addPageTemplates([PageTemplate(id="cols", frames=frames, onPage=decorate)])

    story = [Paragraph(escape(title), title_style)]
    if settings.intro_text:
        story.append(Paragraph(escape(settings.intro_text).replace("\n", "<br/>"), intro_style))
    story.append(Spacer(1, 3 * mm))
    current_cat = None
    for e in exts:
        cat = settings.category_for(e.number) if settings.categories else ""
        if settings.categories and cat != current_cat:
            current_cat = cat
            story.append(Paragraph(escape(cat or _("Other")), cat_style))
        line = f"<b>{escape(e.number_label)}</b>&nbsp;&nbsp;{escape(entry_name(e))}"
        extras = []
        if settings.show_location and e.location_hint:
            extras.append(escape(e.location_hint))
        if e.type in SERVICE_TYPES:
            extras.append(escape(str(e.get_type_display())))
        if extras:
            line += f" <font color='#555555' size='7.5'>{' · '.join(extras)}</font>"
        story.append(Paragraph(line, entry_style))
    if not exts:
        story.append(Paragraph(escape(_("No entries yet.")), entry_style))
    doc.build(story)
    return doc.filename.getvalue()


def render_json(event, exts=None, q=None) -> list[dict]:
    return as_dicts(event, exts=exts, q=q)


EXPORTERS = {
    "csv": (render_csv, "text/csv; charset=utf-8", "phonebook-{slug}.csv"),
    "vcf": (render_vcf, "text/vcard; charset=utf-8", "phonebook-{slug}.vcf"),
    "ldif": (render_ldif, "text/plain; charset=utf-8", "phonebook-{slug}.ldif"),
    "pdf": (render_pdf, "application/pdf", "phonebook-{slug}.pdf"),
}


def export(event, fmt: str, exts=None) -> tuple[bytes, str, str]:
    """``(body, content_type, filename)`` for one of :data:`EXPORTERS`."""
    render, ctype, fname = EXPORTERS[fmt]
    return render(event, exts), ctype, fname.format(slug=event.slug)
