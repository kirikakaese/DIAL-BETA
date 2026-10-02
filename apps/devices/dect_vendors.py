"""Built-in DECT vendor table keyed by EMC (Equipment Manufacturer Code).

A 13-digit IPEI is ``EMC (5) + PSN (7) + check digit (1)``; the EMC identifies the manufacturer
(assigned by ETSI). This seed list is deliberately **small and best-effort**: it only lists codes
seen on handsets at previous events and could not be re-verified offline against the ETSI EMC
register. The table is meant to be **crowdsourced** - users who know their handset can add the
vendor for an unknown EMC in the portal (``DECTManufacturer.source == "user"``), orga can curate the
list at ``/e/<slug>/devices/manufacturers/``. Corrections to this file are welcome.

Load with ``manage.py pet_dect_vendors`` (idempotent; never overwrites user-contributed rows).
"""
from __future__ import annotations

# emc -> (vendor name, models hint)
BUILTIN_MANUFACTURERS: dict[str, tuple[str, str]] = {
    "10114": ("Mitel / Aastra / DeTeWe", "600d series: 610d, 620d, 630d, 612d, 622d, 632d, 650c"),
    "00329": ("Gigaset / Siemens", "Gigaset S/SL/E/C series (formerly Siemens Gigaset)"),
    "02826": ("Snom (RTX)", "M25, M65, M70, M80, M85, M90"),
    "00088": ("Ascom", "d41, d43, d62, d63, d81, d83"),
    "01018": ("Panasonic", "KX-TCA / KX-TPA / KX-UDT series"),
    "03214": ("Spectralink / Polycom (KIRK)", "KIRK 5020/5040, Spectralink 72xx/75xx/76xx/77xx"),
}


def load_builtin_manufacturers(*, overwrite_names: bool = False) -> tuple[int, int]:
    """Insert the built-in vendors. Returns ``(created, updated)``.

    Existing rows are left alone unless ``overwrite_names`` is set *and* the row is itself
    ``source=builtin`` - user contributions always win.
    """
    from .models import DECTManufacturer

    created = updated = 0
    for emc, (name, hint) in BUILTIN_MANUFACTURERS.items():
        obj, was_created = DECTManufacturer.objects.get_or_create(
            emc=emc, defaults={"name": name, "models_hint": hint, "source": DECTManufacturer.Source.BUILTIN},
        )
        if was_created:
            created += 1
        elif overwrite_names and obj.source == DECTManufacturer.Source.BUILTIN and (
                obj.name != name or obj.models_hint != hint):
            obj.name, obj.models_hint = name, hint
            obj.save(update_fields=["name", "models_hint", "updated_at"])
            updated += 1
    return created, updated
