"""``/prov/`` URL tree (mounted from ``dial.urls``; deliberately *not* ``PORTAL_MOUNT``).

Order matters: the vendor/MAC route is restricted to known vendor keys so it can never shadow a
token URL (tokens are 32 url-safe chars, vendors are short words). The softphone documents
(``linphone.xml`` / ``acrobits.xml``) and the remote phonebook (``phonebook.xml``) come before the generic
``<token>/<filename>`` route.
"""
from django.urls import path, re_path

from . import prov_views
from .models import ProvisioningProfile

app_name = "prov"

_vendors = "|".join(ProvisioningProfile.Vendor.values)

urlpatterns = [
    path("gsm/register/", prov_views.gsm_register, name="gsm_register"),
    re_path(rf"^(?P<vendor>{_vendors})/(?P<mac>[0-9A-Fa-f:\-]{{12,17}})\.(?P<ext>cfg|xml)$", prov_views.by_mac,
            name="by_mac"),
    re_path(r"^(?P<token>[^/]+)/(?P<client>linphone|acrobits)\.xml$", prov_views.softphone_xml, name="softphone"),
    re_path(r"^(?P<token>[^/]+)/phonebook\.xml$", prov_views.phonebook_xml, name="phonebook"),
    path("<str:token>/<str:filename>", prov_views.by_token, name="by_token"),
]
