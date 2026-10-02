"""Form rendering helpers that make every field screen-reader friendly, whatever form class it comes from.

``{% a11y_field field %}`` renders the bound field's widget with

* ``aria-describedby`` pointing at ``<id>_help`` and/or ``<id>_errors`` (the ids ``portal/_field.html`` emits),
* ``aria-invalid="true"`` when the field has errors.

``AccessibleFormMixin`` (apps.portal.forms) does the same on the Python side; the tag is the safety net for
the many small forms that do not inherit from it.
"""
from django import template

register = template.Library()


@register.simple_tag
def a11y_field(field, **extra):
    """Render ``field`` (a BoundField) with aria-describedby / aria-invalid derived from help text and errors."""
    attrs = dict(extra)
    described = []
    existing = field.field.widget.attrs.get("aria-describedby", "")
    if existing:
        described.extend(existing.split())
    if field.help_text:
        described.append(f"{field.auto_id}_help")
    if field.errors:
        described.append(f"{field.auto_id}_errors")
        attrs["aria-invalid"] = "true"
    if described:
        attrs["aria-describedby"] = " ".join(dict.fromkeys(described))
    return field.as_widget(attrs=attrs)
