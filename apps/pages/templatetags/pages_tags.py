"""``{% load pages_tags %}{% dashboard_pages event %}`` - info pages flagged for the event dashboard."""
from django import template

from apps.pages.models import InfoPage

register = template.Library()


@register.inclusion_tag("pages/_dashboard.html", takes_context=True)
def dashboard_pages(context, event):
    user = context.get("user")
    qs = InfoPage.objects.filter(event=event, show_on_dashboard=True)
    if not (user is not None and user.is_authenticated and user.is_orga(event)):
        qs = qs.filter(published=True)
    return {"event": event, "pages": qs}
