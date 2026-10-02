"""Info pages: orga-editable Markdown pages per event (GURU3-style)."""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel

from .rendering import first_line, render_markdown


class InfoPage(TimeStampedModel):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="pages")
    slug = models.SlugField(help_text=_("Used in the page address, e.g. 'how-to-dect'."))
    title = models.CharField(max_length=120)
    body = models.TextField(blank=True, help_text=_("Markdown. Raw HTML is shown as text, not rendered."))
    order = models.IntegerField(default=0, help_text=_("Lower numbers come first."))
    published = models.BooleanField(default=True, help_text=_("Unpublished pages are only visible to the orga."))
    show_on_dashboard = models.BooleanField(default=False, help_text=_("Also list this page on the event dashboard."))
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        unique_together = [("event", "slug")]
        ordering = ["order", "title"]
        verbose_name = _("Info page")
        verbose_name_plural = _("Info pages")

    def __str__(self):
        return f"{self.title} ({self.slug})"

    @property
    def body_html(self):
        return render_markdown(self.body)

    @property
    def excerpt(self) -> str:
        return first_line(self.body)
