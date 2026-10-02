"""Smoke-test: GET every reachable page as admin/orga/user against the seeded DB.

Usage: .venv/bin/python scripts/smoke.py   (requires `manage.py seed_demo` first)

``page_lists()`` is side-effect free and is reused by ``apps/core/tests/test_a11y.py`` and
``manage.py pet_a11y`` so the accessibility checks cover the same URLs.
"""
import os
import sys
from pathlib import Path

FEATURES = ",".join([
    "phonebook", "callgroups", "voicemail", "stats", "messaging", "ivr", "conferences", "federation",
    "breakout", "emergency", "guest_extensions", "waitlist", "webhooks",
])


def page_lists(S, ext_pk, ext_number, dev_pk, pb_token, vendors=(), prov_token=None):
    """Return ``{"anon": [...], "user": [...], "orga": [...]}`` URL lists for event slug ``S``.

    All arguments are formatted into the URLs verbatim, so placeholders (``"{ext_pk}"``) work too.
    """
    pages_public = ["/", "/accounts/login/", "/accounts/register/", "/events/", "/api/docs/", "/api/schema/",
                    "/docs/", "/docs/event-guide/", "/docs/architecture/", "/docs/?q=number+plan",
                    "/api/v1/health/", f"/api/v1/availability/?event={S}&number=4242",
                    f"/e/{S}/phonebook/", f"/e/{S}/phonebook/export.pdf", f"/e/{S}/phonebook/export.csv",
                    f"/e/{S}/phonebook/export.vcf", f"/e/{S}/phonebook/export.ldif", "/api/v1/federation/directory/",
                    "/manifest.webmanifest", "/sw.js", "/offline/"]
    pages_public += [f"/e/{S}/phonebook/remote/{pb_token}/{v}.xml" for v in vendors]
    pages_public += [f"/e/{S}/phonebook/remote/{pb_token}/snom.xml?q=a"]
    if prov_token:
        pages_public.append(f"/prov/{prov_token}/phonebook.xml")
    pages_user = ["/dashboard/", "/accounts/profile/", "/accounts/profile/tokens/", "/accounts/profile/export/",
                  f"/e/{S}/", f"/e/{S}/extensions/", f"/e/{S}/extensions/new/", f"/e/{S}/extensions/port/",
                  f"/e/{S}/extensions/{ext_pk}/", f"/e/{S}/extensions/{ext_pk}/edit/",
                  f"/e/{S}/extensions/{ext_pk}/transfer/", f"/e/{S}/extensions/{ext_pk}/devices/add/",
                  f"/e/{S}/devices/{dev_pk}/", f"/e/{S}/devices/{dev_pk}/qr.png",
                  f"/e/{S}/devices/{dev_pk}/qr.png?client=linphone", f"/e/{S}/pages/",
                  f"/e/{S}/phonebook/{ext_number}.vcf", f"/e/{S}/phonebook/{ext_number}/qr.png",
                  f"/e/{S}/phonebook/{ext_number}/card/",
                  f"/e/{S}/callback/", f"/e/{S}/callgroups/", f"/e/{S}/voicemail/", f"/e/{S}/stats/mine/",
                  f"/e/{S}/messaging/", f"/e/{S}/ivr/", f"/e/{S}/conferences/",
                  f"/e/{S}/devices/history/", f"/e/{S}/callgroups/mine/", f"/e/{S}/callgroups/invites/",
                  f"/e/{S}/numbering/random/?type=dect", f"/api/v1/random-number/?event={S}&type=dect",
                  "/api/v1/me/", f"/api/v1/extensions/?event__slug={S}", f"/api/v1/devices/?event__slug={S}",
                  f"/api/v1/events/{S}/", f"/api/v1/events/{S}/number-plan/", f"/api/v1/phonebook/?event={S}"]
    pages_orga = [f"/e/{S}/orga/", f"/e/{S}/orga/settings/", f"/e/{S}/orga/clone/", f"/e/{S}/orga/numberplan/",
                  f"/e/{S}/orga/numberplan/ranges/new/", f"/e/{S}/orga/queue/", f"/e/{S}/orga/extensions/",
                  f"/e/{S}/orga/members/", f"/e/{S}/orga/groups/", f"/e/{S}/orga/guests/",
                  f"/e/{S}/orga/guests/?print=1", f"/e/{S}/orga/audit/", f"/e/{S}/orga/webhooks/",
                  f"/e/{S}/orga/export/", f"/e/{S}/orga/import/", f"/e/{S}/orga/helpdesk/?q=alice",
                  f"/e/{S}/orga/tokens/", f"/e/{S}/pbx/", f"/e/{S}/orga/import-csv/",
                  f"/e/{S}/orga/import-csv/sample.csv", f"/e/{S}/orga/helpdesk/?q={ext_number}",
                  f"/e/{S}/pages/manage/", f"/e/{S}/pages/manage/new/",
                  f"/api/v1/pages/?event={S}", f"/api/v1/extensions/history/?event={S}&number={ext_number}",
                  f"/api/v1/pbx/connection/?event={S}", "/admin/pages/infopage/", "/admin/pbx/pbxconnection/",
                  f"/e/{S}/dect/", f"/e/{S}/dect/handsets/", f"/e/{S}/dect/map/",
                  f"/e/{S}/dect/alerts/", f"/e/{S}/dect/survey/", f"/e/{S}/stats/", f"/e/{S}/callback/all/",
                  f"/e/{S}/voicemail/all/", f"/e/{S}/emergency/", f"/e/{S}/federation/", f"/e/{S}/breakout/",
                  f"/e/{S}/messaging/broadcast/",
                  f"/e/{S}/numbering/settings/", f"/e/{S}/numbering/pools/", f"/e/{S}/numbering/claims/",
                  f"/e/{S}/devices/manufacturers/", f"/e/{S}/devices/gsm/",
                  f"/api/v1/pbx/outbox/?event={S}", "/admin/pbx/pbxjob/", "/admin/accounts/registrationemailtoken/",
                  f"/api/v1/phonebook/directory/?event={S}", f"/e/{S}/phonebook/settings/",
                  "/admin/phonebook/phonebooksettings/",
                  f"/api/v1/dect/rfps/?event__slug={S}", f"/api/v1/dect/coverage/?event={S}",
                  f"/api/v1/stats/summary/?event={S}", f"/api/v1/pbx/status/?event={S}",
                  f"/api/v1/pbx/snapshot/?event={S}", f"/api/v1/pbx/snapshot/schema/?event={S}",
                  f"/api/v1/events/{S}/audit/", f"/api/v1/events/{S}/export/", "/admin/",
                  "/admin/extensions/extension/"]
    return {"anon": pages_public, "user": pages_user, "orga": pages_orga}


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pet.settings.dev")
    os.environ.setdefault("PET_FEATURES", FEATURES)
    import django

    django.setup()

    from django.test import Client

    from apps.accounts.models import User
    from apps.devices.models import Device
    from apps.events.models import Event
    from apps.extensions.models import Extension
    from apps.phonebook import remote as pb_remote
    from apps.phonebook import services as pb_services

    event = Event.objects.get(slug="demo")
    admin = User.objects.get(email="admin@pet.local")
    alice = User.objects.get(email="alice@pet.local")
    ext = Extension.objects.filter(event=event, owner=alice, state="active").first()
    dev = Device.objects.filter(event=event, owner=alice).first()
    requested = Extension.objects.filter(event=event, state="requested").first()
    pb_token = pb_services.get_settings(event).directory_token

    pages = page_lists(event.slug, ext.pk, ext.number, dev.pk, pb_token, vendors=pb_remote.VENDORS,
                       prov_token=dev.provisioning_token if dev is not None else None)
    failures = []

    def run(client, urls, who):
        for url in urls:
            try:
                r = client.get(url, follow=True)
                code = r.status_code
            except Exception as exc:  # noqa: BLE001
                code = f"EXC {type(exc).__name__}: {exc}"
            ok = code == 200
            print(f"{'OK ' if ok else 'FAIL'} [{who}] {code} {url}")
            if not ok:
                failures.append((who, url, code))

    run(Client(), pages["anon"], "anon")
    c = Client()
    c.force_login(alice)
    run(c, pages["user"], "alice")
    c = Client()
    c.force_login(admin)
    run(c, pages["orga"], "admin")

    # Moderation round trip via the orga UI (skipped when a previous run already drained the queue)
    if requested is not None:
        c.post(f"/e/{event.slug}/orga/queue/{requested.pk}/approve/", {"note": "smoke"}, follow=True)
        requested.refresh_from_db()
        print("approve flow:", requested.state)
        if requested.state != "active":
            failures.append(("admin", "approve", requested.state))

    print("\nFAILURES:", len(failures))
    for f in failures:
        print("  ", f)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
