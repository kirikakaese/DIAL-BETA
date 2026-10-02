"""Test URLconf: project URLs plus stubs for the feature-app namespaces ``templates/base.html``
links to. Only namespaces that do not exist yet are stubbed (same approach as ``apps/callback/tests``)."""
from django.http import HttpResponse
from django.urls import get_resolver, include, path

from dial.urls import urlpatterns as project_patterns

_existing = set(get_resolver("dial.urls").namespace_dict)


def _stub(request, slug):
    return HttpResponse("stub")


def _mount(label):
    return path(f"e/<slug:slug>/{label}/", include(([path("", _stub, name="index")], label), namespace=label))


urlpatterns = list(project_patterns) + [
    _mount(label) for label in ("phonebook", "callgroups", "voicemail", "callback") if label not in _existing
]
