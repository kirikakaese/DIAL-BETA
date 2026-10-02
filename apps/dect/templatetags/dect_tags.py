from django import template

from apps.dect.reverse import dect_reverse

register = template.Library()


@register.simple_tag
def dect_url(name, *args, **kwargs):
    """``{% dect_url 'index' event.slug %}`` - like ``{% url 'dect:index' ... %}`` but mount-agnostic."""
    return dect_reverse(name, args=args, kwargs=kwargs)
