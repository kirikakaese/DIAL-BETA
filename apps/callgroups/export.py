"""Event export hook: ``apps.events.export.export_event`` merges this dict."""
from .models import CallGroup


def export_event(event) -> dict:
    out = []
    for g in CallGroup.objects.filter(event=event).select_related("extension", "user_group").prefetch_related(
            "members__extension", "admins"):
        out.append({
            "number": g.extension.number, "name": g.name, "strategy": g.strategy, "ring_timeout": g.ring_timeout,
            "wrap_up_seconds": g.wrap_up_seconds, "allow_self_service": g.allow_self_service,
            "description": g.description, "user_group": g.user_group.slug if g.user_group else None,
            "shortcode": g.shortcode,
            "admins": sorted(a.username for a in g.admins.all()),
            "members": [{"number": m.extension.number, "logged_in": m.logged_in, "priority": m.priority,
                         "delay_s": m.delay_s}
                        for m in g.members.all()],
        })
    return {"callgroups": out}
