"""Celery application for DIAL.

Used for provisioning jobs (OMM/PBX push), callback/ringback scheduling,
wake-up calls, CDR aggregation and alerting.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dial.settings.dev")

app = Celery("dial")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.on_after_configure.connect
def _setup_pbx_outbox_schedule(sender, **kwargs):
    """Beat entries for the durable PBX outbox (``apps.pbx.outbox``), added next to ``CELERY_BEAT_SCHEDULE``."""
    sender.add_periodic_task(5.0, sender.signature("apps.pbx.tasks.drain_outbox"), name="pbx-outbox-drain")
    sender.add_periodic_task(86400.0, sender.signature("apps.pbx.tasks.purge_delivered_jobs"),
                             name="pbx-outbox-purge-delivered")
