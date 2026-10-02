"""``manage.py seed_demo`` - create the *Demo Camp* event with realistic fake data.

The command is idempotent: running it twice does not duplicate anything. ``--reset`` first deletes
everything that belongs to the demo (event, demo users, PBX realtime rows) and then recreates it.

Every feature block is wrapped in ``try/except`` and imports its service functions lazily, so a
disabled feature flag or a missing function only produces a warning instead of aborting the seed.

Demo credentials: ``admin@pet.local`` / ``admin`` (superuser), all other demo users use
``demo1234!``.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import random

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.utils import timezone

log = logging.getLogger("pet.seed")

DEMO_SLUG = "demo"
DEMO_DOMAIN = "pet.local"
ADMIN_EMAIL = f"admin@{DEMO_DOMAIN}"
ADMIN_PASSWORD = "admin"
DEMO_PASSWORD = "demo1234!"
DEMO_USERS = ["alice", "bob", "carol", "dave", "orga"]

# number, type, owner, display name, location hint
EXTENSIONS = [
    ("1000", "dect", "orga", "Orga hotline", "Orga tent"),
    ("1001", "sip", "orga", "Orga office", "Orga container"),
    ("1010", "dect", "admin", "NOC", "NOC tent"),
    ("2001", "dect", "alice", "Alice", "Heaven"),
    ("2002", "dect", "bob", "Bob", "Heaven"),
    ("2010", "sip", "alice", "Angel desk", "Heaven"),
    ("4242", "sip", "alice", "Alice (desk)", "Hackcenter, table 12"),
    ("4711", "dect", "bob", "Bob mobile", "Camping north"),
    ("4300", "dect", "carol", "Carol", "Medic tent"),
    ("4301", "sip", "carol", "Medic desk", "Medic tent"),
    ("4400", "sip", "dave", "Dave", "Workshop tent"),
    ("4401", "dect", "dave", "Dave mobile", ""),
    ("4500", "dect", "alice", "Bar", "Bar tent"),
    ("4501", "sip", "bob", "Kitchen", "Food court"),
    ("4502", "dect", "carol", "Night shift", "Medic tent"),
    ("4503", "sip", "dave", "Workshop desk", "Workshop tent"),
    ("4504", "dect", "alice", "Lounge", "Lounge"),
    ("4505", "sip", "bob", "Stage manager", "Main stage"),
    ("4506", "dect", "carol", "First aid mobile", ""),
    ("4507", "sip", "dave", "Radio desk", "Radio tent"),
    ("4508", "dect", "admin", "Infrastructure", "NOC tent"),
    ("4509", "sip", "admin", "Power team", "Generator"),
    ("6001", "sip", "admin", "Infodesk", "Info desk"),
    ("6002", "dect", "admin", "Security", "Gate"),
]

# numbers that stay in the moderation queue (8xxx requires approval, 7777 is vanity)
REQUESTED = [
    ("8001", "dect", "alice"),
    ("8002", "sip", "bob"),
    ("8003", "dect", "carol"),
    ("7777", "dect", "dave"),
]

GUEST_NUMBERS = ["7770", "7771"]

# RFP name -> (pos_x %, pos_y %) on the generated venue map
RFP_POSITIONS = {
    "RFP-Main-Stage": (22.0, 30.0),
    "RFP-Foodcourt": (50.0, 42.0),
    "RFP-Infodesk": (50.0, 78.0),
    "RFP-Camp-North": (80.0, 20.0),
    "RFP-Camp-South": (80.0, 62.0),
    "RFP-Workshop": (22.0, 72.0),
}

# label, (x0, y0, x1, y1) on a 1200x800 canvas, fill colour
MAP_AREAS = [
    ("Main stage", (120, 120, 420, 340), (70, 90, 130)),
    ("Food court", (500, 260, 720, 420), (110, 90, 60)),
    ("Info desk", (520, 560, 700, 680), (60, 110, 90)),
    ("Camping north", (820, 80, 1120, 300), (80, 100, 80)),
    ("Camping south", (820, 400, 1120, 660), (80, 100, 80)),
    ("Workshop tent", (120, 520, 400, 720), (110, 70, 110)),
]


class Command(BaseCommand):
    help = (
        "Create (or reset) the 'Demo Camp' event: users, extensions, devices, groups, statistics."
    )

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete demo data first.")
        parser.add_argument("--slug", default=DEMO_SLUG, help="Event slug (default: demo).")
        parser.add_argument("--no-cdr", action="store_true", help="Skip synthetic call records.")

    # ------------------------------------------------------------------ plumbing
    def _say(self, msg):
        if self.verbosity:
            self.stdout.write(msg)

    def _warn(self, msg):
        if self.verbosity:
            self.stdout.write(self.style.WARNING(msg))
        log.warning(msg)

    def _block(self, label, fn, *args, **kwargs):
        """Run one feature block; log and continue on any error."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the seed must never abort because of one feature
            self._warn(f"  ! {label} skipped: {exc.__class__.__name__}: {exc}")
            log.debug("seed block %s failed", label, exc_info=True)
            return None

    def handle(self, *args, **opts):
        self.verbosity = opts.get("verbosity", 1)
        self.slug = opts["slug"]
        self.rng = random.Random(4242)
        self.summary: list[tuple[str, str]] = []
        self.notes: list[str] = []

        # Run provisioning tasks inline in this process, whatever the deployment's Celery mode is.
        # With the dummy DECT backend this keeps subscriptions in *this* process so the
        # infrastructure
        # sync below sees the handsets. Also avoids racing a worker while we are still writing rows.
        try:
            from pet.celery import app as celery_app

            celery_app.conf.task_always_eager = True
            celery_app.conf.task_eager_propagates = False
        except Exception as exc:  # noqa: BLE001
            self._warn(f"could not force eager Celery: {exc}")

        if opts["reset"]:
            self._reset()

        users = self.seed_users()
        event = self.seed_event(users)
        groups = self.seed_groups(event, users)
        self.seed_number_plan(event, groups)
        exts = self.seed_extensions(event, users)
        self._block("devices", self.seed_devices, event, users, exts)
        self._block("moderation queue", self.seed_requested, event, users)
        self._block("guest extensions", self.seed_guests, event, users)
        self._block("call groups", self.seed_callgroups, event, users, exts)
        self._block("announcement", self.seed_announcement, event, users)
        self._block("conference", self.seed_conference, event, users)
        self._block("voicemail", self.seed_voicemail, event, exts)
        self._block("emergency targets", self.seed_emergency, event, users, exts)
        self._block("wake-up call", self.seed_wakeup, event, users, exts)
        self._block("webhook", self.seed_webhook, event)
        self._block("service account", self.seed_service_account, users)
        self._block("DECT infrastructure", self.seed_dect, event)
        self._block("venue map", self.seed_venue_map, event)
        if not opts["no_cdr"]:
            self._block("call records", self.seed_cdrs, event)
        self._block("PBX resync", self.resync_pbx, event)
        self.print_summary(event)

    # ------------------------------------------------------------------ reset
    def _reset(self):
        from apps.accounts.models import User
        from apps.events.models import Event

        self._say(f"Resetting demo data for event '{self.slug}' ...")
        ev = Event.objects.filter(slug=self.slug).first()
        if ev is not None:
            self._block("PBX realtime cleanup", self._reset_pbx_rows, ev)
            ev.delete()
        User.objects.filter(email__endswith=f"@{DEMO_DOMAIN}").delete()

    def _reset_pbx_rows(self, event):
        from apps.pbx import dialplan as dp
        from apps.pbx.models import DialplanEntry, PsAor, PsAuth, PsEndpoint, VoicemailUser

        ctx = dp.event_context(event)
        DialplanEntry.objects.filter(context=ctx).delete()
        VoicemailUser.objects.filter(context=ctx).delete()
        ids = list(
            PsEndpoint.objects.filter(accountcode=event.slug[:20]).values_list("id", flat=True)
        )
        PsEndpoint.objects.filter(id__in=ids).delete()
        PsAuth.objects.filter(id__in=ids).delete()
        PsAor.objects.filter(id__in=ids).delete()

    # ------------------------------------------------------------------ blocks
    def seed_users(self):
        from apps.accounts.models import User

        users = {}
        admin = User.objects.filter(email=ADMIN_EMAIL).first()
        if admin is None:
            admin = User.objects.create_superuser(
                email=ADMIN_EMAIL,
                username="admin",
                password=ADMIN_PASSWORD,
                display_name="Demo Admin",
            )
        users["admin"] = admin
        for name in DEMO_USERS:
            u = User.objects.filter(email=f"{name}@{DEMO_DOMAIN}").first()
            if u is None:
                u = User.objects.create_user(
                    email=f"{name}@{DEMO_DOMAIN}",
                    username=name,
                    password=DEMO_PASSWORD,
                    display_name=name.capitalize(),
                    email_verified=True,
                )
            users[name] = u
        self.summary.append(("Users", f"{len(users)} (admin + {', '.join(DEMO_USERS)})"))
        return users

    def seed_event(self, users):
        from apps.events.models import Event, EventMembership

        today = timezone.localdate()
        event, created = Event.objects.get_or_create(
            slug=self.slug,
            defaults={
                "name": "Demo Camp",
                "description": "A fictional hacker camp used to demonstrate PET. Everything is fake.",
                "state": Event.State.LIVE,
                "start_date": today,
                "end_date": today + dt.timedelta(days=5),
                "location": "Demo Field",
                "timezone": "Europe/Berlin",
                "sip_domain": f"{self.slug}.{DEMO_DOMAIN}",
                "primary_color": "#f97316",
                "accent_color": "#22d3ee",
                "announcement": "Welcome to Demo Camp! Dial 9003 for an echo test, 9000 for a test "
                "call-back, "
                "*66<number> to be called back when a busy extension is free.",
                "allow_guest_extensions": True,
                # demo users hold more numbers than a real attendee would
                "max_extensions_per_user": 10,
                "cdr_retention_days": 30,
            },
        )
        if created:
            EventMembership.objects.get_or_create(
                event=event, user=users["admin"], defaults={"role": "admin"}
            )
        self.summary.append(
            (
                "Event",
                f"{event.name} ({event.slug}, {event.state}, {event.start_date}..{event.end_date})",
            )
        )
        return event

    def seed_groups(self, event, users):
        from apps.events.models import EventMembership, UserGroup

        groups = {}
        for slug, name, desc in [
            ("angels", "Angels", "Volunteers / staff"),
            ("medics", "Medics", "On-site medical team"),
            ("orga", "Orga", "Organisation team"),
        ]:
            groups[slug], _c = UserGroup.objects.get_or_create(
                event=event, slug=slug, defaults={"name": name, "description": desc}
            )
        memberships = {
            "orga": ("orga", ["orga"]),
            "alice": ("user", ["angels"]),
            "bob": ("user", ["angels"]),
            "carol": ("user", ["medics"]),
            "dave": ("user", []),
            "admin": ("admin", ["orga"]),
        }
        for uname, (role, gslugs) in memberships.items():
            m, _c = EventMembership.objects.get_or_create(
                event=event, user=users[uname], defaults={"role": role}
            )
            if gslugs:
                m.groups.add(*[groups[g] for g in gslugs])
        self.summary.append(("User groups", ", ".join(groups)))
        return groups

    def seed_number_plan(self, event, groups):
        from apps.numbering.models import NumberPlan, NumberRange

        plan, _c = NumberPlan.objects.get_or_create(event=event)
        plan.min_length = plan.max_length = 4
        plan.test_ringback_number = "9000"
        plan.wakeup_service_number = "9001"
        plan.site_survey_number = "9002"
        plan.echo_test_number = "9003"
        plan.voicemail_number = "9999"
        plan.dect_claim_number = "9004"
        plan.emergency_numbers = ["112", "110"]
        plan.save()
        ranges = [
            dict(
                name="Trunk prefix",
                prefix="0",
                mode="blocked",
                priority=1,
                description="Reserved for breakout / trunks",
            ),
            dict(
                name="Services",
                prefix="9",
                mode="blocked",
                priority=1,
                description="PET service numbers (echo, ringback, wake-up, voicemail)",
            ),
            dict(
                name="Vanity",
                pattern=r"(\d)\1{3}",
                is_vanity=True,
                priority=5,
                description="Repeated digits need manual approval",
            ),
            dict(
                name="Orga",
                prefix="1",
                mode="restricted",
                allowed_roles=["orga", "admin"],
                priority=10,
                description="Orga / infrastructure only",
            ),
            dict(
                name="Angels",
                prefix="2",
                mode="restricted",
                priority=20,
                _groups=["angels"],
                description="Volunteers only",
            ),
            dict(
                name="Premium",
                prefix="8",
                mode="approval",
                priority=30,
                description="Short-dial requests reviewed by the orga team",
            ),
        ]
        for r in ranges:
            gslugs = r.pop("_groups", [])
            rng, _c = NumberRange.objects.get_or_create(plan=plan, name=r["name"], defaults=r)
            if gslugs:
                rng.allowed_groups.set([groups[g] for g in gslugs])
        self.summary.append(
            (
                "Number plan",
                f"{plan.min_length} digits, {plan.ranges.count()} ranges, "
                f"emergency {', '.join(plan.emergency_numbers)}",
            )
        )
        return plan

    def _register(self, event, user, number, ext_type, **fields):
        """Register ``number`` unless a live extension with that number already exists."""
        from apps.extensions.models import Extension
        from apps.extensions.services import register

        existing = (
            Extension.objects.filter(event=event, number=number)
            .exclude(
                state__in=[
                    Extension.State.DELETED,
                    Extension.State.REJECTED,
                    Extension.State.EXPIRED,
                ]
            )
            .first()
        )
        if existing is not None:
            return existing, False
        return register(event, user, number, ext_type, **fields), True

    def seed_extensions(self, event, users):
        exts = {}
        created = 0
        for number, ext_type, owner, name, location in EXTENSIONS:
            ext, was_created = self._register(
                event,
                users[owner],
                number,
                ext_type,
                display_name=name,
                location_hint=location,
                in_phonebook=True,
                description=f"{name} ({ext_type.upper()})",
            )
            exts[number] = ext
            created += was_created
        self.summary.append(("Endpoint extensions", f"{len(exts)} ({created} new)"))
        return exts

    def seed_devices(self, event, users, exts):
        from apps.devices.models import Device, DeviceBinding
        from apps.extensions.tasks import provision_extension

        n_dect = n_sip = 0
        for i, (number, ext_type, owner, name, _loc) in enumerate(EXTENSIONS):
            ext = exts[number]
            if ext.bindings.exists():
                continue
            if ext_type == "dect":
                ipei = f"00077{12345000 + i:08d}"  # 5 digit EMC + 8 digits = 13 digits
                dev, _c = Device.objects.get_or_create(
                    event=event,
                    ipei=ipei,
                    defaults={
                        "type": "dect",
                        "owner": users[owner],
                        "name": f"{name} handset",
                        "handset_model": self.rng.choice(
                            ["Mitel 612d", "Mitel 622d", "Mitel 650c", "Gigaset SL450"]
                        ),
                    },
                )
                dev.ensure_sip_credentials(save=False)
                dev.issue_subscription_pin()
                n_dect += 1
            else:
                dev = Device.objects.create(
                    event=event,
                    type="sip",
                    owner=users[owner],
                    name=f"{name} SIP",
                    sip_transport=self.rng.choice(["udp", "udp", "tcp", "tls"]),
                    sip_user_agent=self.rng.choice(
                        ["Linphone/5.2", "Zoiper 5", "snom D785", "Yealink T46U"]
                    ),
                )
                dev.ensure_sip_credentials()
                n_sip += 1
            DeviceBinding.objects.get_or_create(extension=ext, device=dev)
            provision_extension.delay(str(ext.pk))
        # A multi-device extension: Alice's desk number also rings her handset.
        alice_handset = Device.objects.filter(
            event=event, type="dect", bindings__extension=exts["2001"]
        ).first()
        if alice_handset is not None:
            _b, created = DeviceBinding.objects.get_or_create(
                extension=exts["4242"],
                device=alice_handset,
                defaults={"priority": 1, "ring_delay": 5},
            )
            if created:
                provision_extension.delay(str(exts["4242"].pk))
        self.summary.append(
            (
                "Devices",
                f"{Device.objects.filter(event=event, type='dect').count()} DECT, "
                f"{Device.objects.filter(event=event, type='sip').count()} SIP"
                f" ({n_dect + n_sip} new)",
            )
        )

    def seed_requested(self, event, users):
        from apps.extensions.models import Extension

        for number, ext_type, owner in REQUESTED:
            self._register(
                event,
                users[owner],
                number,
                ext_type,
                request_note="Please! It's my lucky number."
                if number == "7777"
                else "Short number for the helpdesk poster.",
            )
        n = Extension.objects.filter(event=event, state=Extension.State.REQUESTED).count()
        self.summary.append(
            ("Moderation queue", f"{n} requested ({', '.join(r[0] for r in REQUESTED)})")
        )

    def seed_guests(self, event, users):
        from apps.extensions.models import Extension
        from apps.extensions.services import create_guest_extension

        tokens = []
        for number in GUEST_NUMBERS:
            ext = (
                Extension.objects.filter(event=event, number=number, is_temporary=True)
                .exclude(state__in=[Extension.State.DELETED, Extension.State.EXPIRED])
                .first()
            )
            if ext is None:
                ext = create_guest_extension(event, number, users["orga"])
            if ext.claim_token:
                tokens.append(f"{number}: /claim/{ext.claim_token}/")
        self.summary.append(("Guest extensions", ", ".join(GUEST_NUMBERS)))
        self.notes.extend(tokens)

    def seed_callgroups(self, event, users, exts):
        from apps.callgroups.models import CallGroup
        from apps.callgroups.services import add_member, create_group

        def group(number, name, strategy, members, owner, **kw):
            g = CallGroup.objects.filter(event=event, extension__number=number).first()
            if g is None:
                g = create_group(
                    event, users[owner], number, name, strategy, in_phonebook=True, **kw
                )
            for m in members:
                add_member(g, exts[m], users[owner], via="auto")
            return g

        group(
            "3000",
            "Helpdesk",
            "ringall",
            ["2001", "2002", "1000"],
            "orga",
            description="Phone helpdesk - ring all logged-in members",
            ring_timeout=25,
        )
        group(
            "3001",
            "Medics",
            "roundrobin",
            ["4300", "4301", "4401"],
            "orga",
            description="Medical team, round robin",
            wrap_up_seconds=60,
        )
        n = CallGroup.objects.filter(event=event).count()
        self.summary.append(
            ("Call groups", f"{n} (3000 Helpdesk ring-all, 3001 Medics round-robin)")
        )

    def seed_announcement(self, event, users):
        from apps.ivr.models import Announcement
        from apps.ivr.services import create_announcement

        if not Announcement.objects.filter(
            extension__event=event, extension__number="4000"
        ).exists():
            create_announcement(
                event,
                users["orga"],
                "4000",
                tts_text="Welcome to Demo Camp",
                language="en",
                display_name="Welcome message",
                description="TTS announcement",
            )
        self.summary.append(("Announcement", "4000 (TTS 'Welcome to Demo Camp')"))

    def seed_conference(self, event, users):
        from apps.conferences.models import ConferenceRoom
        from apps.conferences.services import create_room

        if not ConferenceRoom.objects.filter(
            extension__event=event, extension__number="5000"
        ).exists():
            create_room(
                event,
                users["orga"],
                "5000",
                pin="1234",
                name="Orga conference",
                display_name="Conference",
                description="Dial in, PIN 1234",
            )
        self.summary.append(("Conference room", "5000 (PIN 1234)"))

    def seed_voicemail(self, event, exts):
        from apps.voicemail.services import ensure_mailbox, update_mailbox

        boxes = 0
        for number, pin in [("4242", "1234"), ("4711", "2468"), ("4300", "1357"), ("1000", "0000")]:
            mb = ensure_mailbox(exts[number])
            if mb.pin != pin:
                update_mailbox(mb, pin=pin)
            boxes += 1
        self.summary.append(("Voicemail boxes", f"{boxes} (4242 PIN 1234, 4711, 4300, 1000)"))

    def seed_emergency(self, event, users, exts):
        from apps.emergency.services import create_target

        create_target(
            event, users["orga"], "112", destination_extension=exts["4300"], fallback_number="3001"
        )
        create_target(
            event, users["orga"], "110", destination_extension=exts["6002"], fallback_number="1000"
        )
        self.summary.append(("Emergency targets", "112 → 4300 (medics), 110 → 6002 (security)"))

    def seed_wakeup(self, event, users, exts):
        from apps.callback.models import ScheduledCall
        from apps.callback.services import schedule_wakeup

        if ScheduledCall.objects.filter(event=event, extension=exts["4711"]).exists():
            return
        when = timezone.now() + dt.timedelta(days=1)
        when = when.replace(hour=8, minute=0, second=0, microsecond=0)
        schedule_wakeup(
            event, users["bob"], exts["4711"], when, announcement_text="Good morning, Bob!"
        )
        self.summary.append(("Wake-up call", f"4711 at {when:%Y-%m-%d %H:%M} UTC"))

    def seed_webhook(self, event):
        from apps.events.models import Webhook

        Webhook.objects.get_or_create(
            event=event,
            name="Demo webhook (inactive)",
            defaults={
                "url": "http://example.invalid/hook",
                "secret": "demo-webhook-secret",
                "is_active": False,
                "event_types": ["extension.approved", "extension.deleted", "dect.rfp.down"],
            },
        )
        self.summary.append(("Webhook", "http://example.invalid/hook (inactive)"))

    def seed_service_account(self, users):
        from apps.accounts.models import ServiceAccount

        if ServiceAccount.objects.filter(owner=users["admin"], name="demo-cli").exists():
            self.summary.append(
                ("Service account", "demo-cli (exists; mint a new token with manage.py pet_token)")
            )
            return
        _acct, raw = ServiceAccount.issue(
            name="demo-cli",
            owner=users["admin"],
            scopes=["*"],
            description="Created by seed_demo for the pet CLI",
        )
        self.summary.append(("Service account", "demo-cli (token printed below, shown only once)"))
        self.notes.append(f"PET_TOKEN={raw}")

    def seed_dect(self, event):
        from apps.dect.models import RFP
        from apps.dect.services import sync_infrastructure

        result = sync_infrastructure(event)
        n = RFP.objects.filter(event=event).count()
        self.summary.append(
            (
                "DECT",
                f"{n} RFPs, {result.get('handsets', 0)} handsets seen, "
                f"{result.get('alerts', 0)} alerts",
            )
        )

    def seed_venue_map(self, event):
        from apps.dect.models import RFP, VenueMap

        vmap = VenueMap.objects.filter(event=event, name="Demo Field").first()
        if vmap is None:
            png = _render_map()
            name = default_storage.save(f"venue_maps/{event.slug}-demo-field.png", ContentFile(png))
            vmap = VenueMap.objects.create(
                event=event, name="Demo Field", image=name, width=1200, height=800, is_default=True
            )
        placed = 0
        for rfp in RFP.objects.filter(event=event):
            pos = RFP_POSITIONS.get(rfp.name)
            if pos and (rfp.venue_map_id != vmap.pk or rfp.pos_x is None):
                rfp.venue_map, (rfp.pos_x, rfp.pos_y) = vmap, pos
                rfp.save(update_fields=["venue_map", "pos_x", "pos_y", "updated_at"])
            placed += bool(pos)
        self.summary.append(("Venue map", f"{vmap.image.name} ({placed} RFPs placed)"))

    def seed_cdrs(self, event):
        from apps.dect.models import RFP
        from apps.extensions.models import Extension
        from apps.stats.models import CallRecord
        from apps.stats.services import aggregate_hourly, ingest_cdr

        if CallRecord.objects.filter(event=event).count() >= 100:
            self.summary.append(
                ("Call records", f"{CallRecord.objects.filter(event=event).count()} (kept)")
            )
            aggregate_hourly(event)
            return
        numbers = list(
            Extension.objects.filter(event=event)
            .active()
            .exclude(is_temporary=True)
            .values_list("number", flat=True)
        )
        rfps = list(RFP.objects.filter(event=event).values_list("omm_id", flat=True)) or [""]
        now = timezone.now()
        created = 0
        for i in range(300):
            # busier in the evening: bias the start hour
            hours_ago = self.rng.triangular(0, 48, 30)
            start = now - dt.timedelta(hours=hours_ago, seconds=self.rng.randint(0, 3599))
            src, dst = self.rng.sample(numbers, 2)
            if self.rng.random() < 0.08:
                dst = self.rng.choice(["3000", "3001", "4000", "9003"])
            roll = self.rng.random()
            if roll < 0.68:
                disposition, ring, talk = (
                    "ANSWERED",
                    self.rng.randint(2, 20),
                    int(self.rng.expovariate(1 / 120)) + 5,
                )
            elif roll < 0.88:
                disposition, ring, talk = "NO ANSWER", self.rng.randint(15, 45), 0
            elif roll < 0.96:
                disposition, ring, talk = "BUSY", self.rng.randint(1, 4), 0
            else:
                disposition, ring, talk = "FAILED", self.rng.randint(1, 3), 0
            answer = start + dt.timedelta(seconds=ring) if talk else None
            end = start + dt.timedelta(seconds=ring + talk)
            rec = ingest_cdr(
                event,
                {
                    "src": src,
                    "dst": dst,
                    "start": start.isoformat(),
                    "answer": answer.isoformat() if answer else "",
                    "end": end.isoformat(),
                    "duration": ring + talk,
                    "billsec": talk,
                    "disposition": disposition,
                    "channel": f"PJSIP/{event.slug}-{src}-{i:08x}",
                    "dstchannel": f"PJSIP/{event.slug}-{dst}-{i:08x}",
                    "uniqueid": f"seed-{event.slug}-{i:04d}",
                    "rfp": self.rng.choice(rfps),
                },
            )
            created += rec is not None
        hours = aggregate_hourly(event)
        self.summary.append(
            ("Call records", f"{created} synthetic CDRs over 48h, {hours} hourly buckets")
        )

    def resync_pbx(self, event):
        from apps.pbx import get_pbx

        pbx = get_pbx(event)
        n = pbx.sync_event(event)
        self.summary.append(("PBX", f"{pbx.name}: {n} extensions synced"))

    # ------------------------------------------------------------------ output
    def print_summary(self, event):
        if not self.verbosity:
            return
        width = max(len(k) for k, _v in self.summary)
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Demo data for '{event.slug}' is ready"))
        self.stdout.write("-" * (width + 60))
        for k, v in self.summary:
            self.stdout.write(f"{k.ljust(width)}  {v}")
        self.stdout.write("-" * (width + 60))
        self.stdout.write(
            f"Login:  {ADMIN_EMAIL} / {ADMIN_PASSWORD}   (users: "
            f"{', '.join(u + '@' + DEMO_DOMAIN for u in DEMO_USERS)} / {DEMO_PASSWORD})"
        )
        self.stdout.write(
            f"Portal: /e/{event.slug}/   Orga: /e/{event.slug}/orga/   API docs: /api/docs/"
        )
        for note in self.notes:
            self.stdout.write(f"  {note}")
        self.stdout.write("")


def _render_map() -> bytes:
    """A simple 1200x800 site plan: grey background, coloured areas with labels."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1200, 800), (58, 61, 68))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 26)
        small = ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        try:
            font, small = ImageFont.load_default(size=26), ImageFont.load_default(size=18)
        except TypeError:  # Pillow < 10.1
            font = small = ImageFont.load_default()
    # grid
    for x in range(0, 1200, 100):
        draw.line([(x, 0), (x, 800)], fill=(70, 74, 82), width=1)
    for y in range(0, 800, 100):
        draw.line([(0, y), (1200, y)], fill=(70, 74, 82), width=1)
    # a "road"
    draw.rectangle((460, 0, 500, 800), fill=(90, 90, 95))
    draw.rectangle((0, 460, 1200, 500), fill=(90, 90, 95))
    for label, box, colour in MAP_AREAS:
        draw.rectangle(box, fill=colour, outline=(220, 220, 220), width=2)
        draw.text((box[0] + 12, box[1] + 10), label, fill=(240, 240, 240), font=font)
    draw.text(
        (20, 760),
        "Demo Field - generated by seed_demo (not a real place)",
        fill=(180, 180, 180),
        font=small,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
