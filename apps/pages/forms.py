"""Orga form for info pages."""
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.portal.forms import PetModelForm

from .models import InfoPage


class InfoPageForm(PetModelForm):
    class Meta:
        model = InfoPage
        fields = ["title", "slug", "body", "order", "published", "show_on_dashboard"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 18, "class": "mono", "spellcheck": "true"}),
        }

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event

    def clean_slug(self):
        slug = self.cleaned_data["slug"]
        if slug in ("manage", "new"):
            raise forms.ValidationError(_("This slug is reserved."))
        qs = InfoPage.objects.filter(event=self.event, slug=slug)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_("A page with this slug already exists in this event."))
        return slug
