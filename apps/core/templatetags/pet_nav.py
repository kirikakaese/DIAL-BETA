"""Navigation helpers for the portal templates.

``{% nav_active 'portal:orga_queue' 'dect:' %}`` renders ``aria-current="page"`` when the
current view name equals one of the arguments or - for arguments ending in ``:`` - lives in
that URL namespace. This lets the sidebar highlight the right section without every view
having to pass an ``active`` variable.
"""
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


def view_name(context) -> str:
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    return getattr(match, "view_name", "") or ""


def matches(current: str, patterns) -> bool:
    for pattern in patterns:
        if not pattern:
            continue
        if pattern.endswith(":"):
            if current.startswith(pattern):
                return True
        elif pattern.endswith("*"):
            if current.startswith(pattern[:-1]):
                return True
        elif current == pattern:
            return True
    return False


@register.simple_tag(takes_context=True)
def nav_active(context, *patterns):
    if matches(view_name(context), patterns):
        return mark_safe('aria-current="page"')
    return ""


@register.simple_tag(takes_context=True)
def nav_is(context, *patterns):
    """Boolean variant for ``{% if %}`` usage: ``{% nav_is 'dect:' as in_dect %}``."""
    return matches(view_name(context), patterns)
