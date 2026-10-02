"""Portal (server-rendered UI) URL root.

Core portal pages live in ``apps.portal.views``. Feature apps expose their own
UI under ``apps.<app>.urls`` with ``app_name`` set and ``PORTAL_MOUNT = True``;
``pet.urls`` mounts them at ``/e/<event_slug>/<app_label>/`` as top-level
namespaces (e.g. ``phonebook:index``).
"""
from django.urls import path

from . import docs, views

app_name = "portal"

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("docs/", docs.index, name="docs_index"),
    path("docs/<slug:slug>/", docs.page, name="docs_page"),
    path("events/", views.event_list, name="event_list"),
    path("events/new/", views.event_create, name="event_create"),
    path("switch/<slug:slug>/", views.switch_event, name="switch_event"),
    path("claim/<str:token>/", views.claim_guest, name="claim_guest"),
    path("transfer/<str:token>/", views.accept_transfer, name="accept_transfer"),
    path("e/<slug:slug>/", views.event_dashboard, name="event_dashboard"),
    path("e/<slug:slug>/join/", views.event_join, name="event_join"),
    path("e/<slug:slug>/extensions/", views.extension_list, name="extension_list"),
    path("e/<slug:slug>/extensions/new/", views.extension_create, name="extension_create"),
    path("e/<slug:slug>/extensions/port/", views.extension_port, name="extension_port"),
    path("e/<slug:slug>/extensions/<uuid:pk>/", views.extension_detail, name="extension_detail"),
    path("e/<slug:slug>/extensions/<uuid:pk>/edit/", views.extension_edit, name="extension_edit"),
    path("e/<slug:slug>/extensions/<uuid:pk>/claim-code/", views.extension_new_claim_code,
         name="extension_new_claim_code"),
    path("e/<slug:slug>/extensions/<uuid:pk>/delete/", views.extension_delete, name="extension_delete"),
    path("e/<slug:slug>/extensions/<uuid:pk>/transfer/", views.extension_transfer, name="extension_transfer"),
    path("e/<slug:slug>/extensions/<uuid:pk>/devices/add/", views.device_add, name="device_add"),
    path("e/<slug:slug>/devices/<uuid:pk>/", views.device_detail, name="device_detail"),
    path("e/<slug:slug>/devices/<uuid:pk>/pin/", views.device_new_pin, name="device_new_pin"),
    path("e/<slug:slug>/devices/<uuid:pk>/rotate/", views.device_rotate_sip, name="device_rotate_sip"),
    path("e/<slug:slug>/devices/<uuid:pk>/qr.png", views.device_qr, name="device_qr"),
    path("e/<slug:slug>/devices/<uuid:pk>/unbind/<uuid:ext_pk>/", views.device_unbind, name="device_unbind"),
    path("e/<slug:slug>/waitlist/", views.waitlist_join, name="waitlist_join"),
    # orga / moderation
    path("e/<slug:slug>/orga/", views.orga_dashboard, name="orga_dashboard"),
    path("e/<slug:slug>/orga/settings/", views.orga_event_settings, name="orga_event_settings"),
    path("e/<slug:slug>/orga/state/<str:state>/", views.orga_event_state, name="orga_event_state"),
    path("e/<slug:slug>/orga/clone/", views.orga_event_clone, name="orga_event_clone"),
    path("e/<slug:slug>/orga/numberplan/", views.orga_numberplan, name="orga_numberplan"),
    path("e/<slug:slug>/orga/numberplan/ranges/new/", views.orga_range_edit, name="orga_range_create"),
    path("e/<slug:slug>/orga/numberplan/ranges/<int:pk>/", views.orga_range_edit, name="orga_range_edit"),
    path("e/<slug:slug>/orga/numberplan/ranges/<int:pk>/delete/", views.orga_range_delete,
         name="orga_range_delete"),
    path("e/<slug:slug>/orga/queue/", views.orga_queue, name="orga_queue"),
    path("e/<slug:slug>/orga/queue/<uuid:pk>/<str:decision>/", views.orga_moderate, name="orga_moderate"),
    path("e/<slug:slug>/orga/extensions/", views.orga_extensions, name="orga_extensions"),
    path("e/<slug:slug>/orga/extensions/<uuid:pk>/<str:action>/", views.orga_extension_action,
         name="orga_extension_action"),
    path("e/<slug:slug>/orga/members/", views.orga_members, name="orga_members"),
    path("e/<slug:slug>/orga/groups/", views.orga_groups, name="orga_groups"),
    path("e/<slug:slug>/orga/guests/", views.orga_guests, name="orga_guests"),
    path("e/<slug:slug>/orga/audit/", views.orga_audit, name="orga_audit"),
    path("e/<slug:slug>/orga/webhooks/", views.orga_webhooks, name="orga_webhooks"),
    path("e/<slug:slug>/orga/export/", views.orga_export, name="orga_export"),
    path("e/<slug:slug>/orga/import/", views.orga_import, name="orga_import"),
    path("e/<slug:slug>/orga/import-csv/", views.orga_import_csv, name="orga_import_csv"),
    path("e/<slug:slug>/orga/import-csv/sample.csv", views.orga_import_csv_sample, name="orga_import_csv_sample"),
    path("e/<slug:slug>/orga/helpdesk/", views.helpdesk_lookup, name="helpdesk_lookup"),
    path("e/<slug:slug>/orga/tokens/", views.orga_service_accounts, name="orga_service_accounts"),
    path("e/<slug:slug>/orga/resync/", views.orga_resync, name="orga_resync"),
]
