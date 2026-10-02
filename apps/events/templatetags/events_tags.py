"""Template tags for events: ``{% load events_tags %}``."""
from django import template
from django.utils import formats, timezone
from django.utils.translation import gettext as _

from apps.events.models import Event

register = template.Library()


@register.simple_tag
def next_transition(event):
    """Human-readable line for the next scheduled lifecycle transition (in the event's timezone), or ''.

    Usage: ``{% next_transition event as nxt %}{% if nxt %}<p>{{ nxt }}</p>{% endif %}``
    """
    nxt = event.next_scheduled_transition()
    if nxt is None:
        return ""
    state, when = nxt
    local = timezone.localtime(when, event.tzinfo)
    return _("Next: %(state)s on %(when)s") % {
        "state": Event.State(state).label,
        "when": formats.date_format(local, "j M Y H:i"),
    }
