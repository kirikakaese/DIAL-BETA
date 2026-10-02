# PET - Portable Event Telephone

PET is a self-hostable, open-source (AGPL-3.0) management and self-service layer for **temporary event
phone networks**. It sits on top of a PBX (reference: Asterisk) and a DECT installation (reference: Mitel
SIP-DECT OMM) and takes care of everything around them: multi-tenant events, global user accounts,
extensions governed by an orga-defined number plan, DECT handset and SIP endpoint provisioning, callbacks
and wake-up calls, a live DECT infrastructure dashboard, phonebook, call groups, voicemail, statistics and a
complete REST API. It is inspired by Eventphone's GURU3 (as used at CCC events) but generalised so any
conference, festival, camp or LARP can run its own phone network - with or without internet.

PET runs as **one permanent service under a fixed domain** hosting many events; every event connects
its **own** Asterisk and DECT controller at the venue on `/e/<slug>/pbx/` (see the
[Event Guide](docs/EVENT_GUIDE.md) and [Operator Handbook §2](docs/OPERATOR_HANDBOOK.md)).

## Feature overview

| Spec section | Feature | Status | Where |
|---|---|---|---|
| 1 | Events, global users, extensions, devices | ✅ | `apps/events`, `apps/accounts`, `apps/extensions`, `apps/devices` |
| 2.1 | Multi-event tenancy, lifecycle draft→registration→live→archived, **scheduled transitions** (open/live/archive at a set time via beat), cloning, branding | ✅ | `apps/events` (`Event.transition()`, `clone`, `tasks.apply_scheduled_transitions`), portal `/e/<slug>/orga/` |
| 2.1b | **Per-event info pages** (orga-written Markdown, dashboard placement, in export/clone) | ✅ | `apps/pages` (`/e/<slug>/pages/`, `/e/<slug>/pages/manage/`, `/api/v1/pages/`) |
| 2.2 | Number plan policy engine (lengths, ranges, roles/groups, vanity, quotas), **prefix-free numbering**, random free numbers from **extension pools**, **extension claims** (orga reserves a number for a person), moderation queue, audit log | ✅ | `apps/numbering` (`NumberPlan.evaluate()` → `PolicyResult`, `ExtensionPool`, `ExtensionClaim`), `apps/extensions/services.py`, `apps/core/audit.py` |
| 2.2b | **SIP trunk number blocks** (`4700–4799` to a remote PBX, one SIP account, always moderated, overlap checked both ways) | ✅ | `apps/numbering/blocks.py`, `apps/extensions/models.py` (`ExtensionType.TRUNK`), `apps/pbx/dialplan.py` (`rows_for_trunk`) |
| 2.2c | **CSV import** of numbers, owners, groups and roles (preview → apply, optional account creation with invitation mail) | ✅ | `apps/extensions/csv_import.py`, `/e/<slug>/orga/import-csv/`, `POST /api/v1/extensions/import/`, `pet extensions import` |
| 2.3 | CCBS/CCNR (`*66`/`*86`), test ringback, wake-up calls with retries | ✅ | `apps/callback` |
| 2.3b | **Phone feature codes for forwarding** (`*21<n>`/`*22<n>`/`*23<n>`/`*20`) and **record IVR announcements by phone** (one-time code, shared recordings volume) | ✅ | `apps/extensions/feature_codes.py`, `apps/ivr/services.py` (`begin_phone_recording`), `deploy/asterisk/conf/extensions.conf` |
| 2.4 | DECT (IPEI + PIN → OMM, manufacturer lookup, handset history/reuse, display-name changes without resubscription), SIP credentials + QR onboarding, **softphone QR provisioning** (generic `sip:` URI, Linphone, Acrobits), **autoprovisioning served by PET** (`/prov/<token>/…`, Snom/Yealink/Grandstream/Cisco), **GSM handsets** via PJSIP trunk, endpoint type registry, multi-device ring | ✅ | `apps/devices` (`endpoint_types.py`, `softphone.py`, `ProvisioningProfile`, `DECTManufacturer`, `prov_views.py`), `apps/dect/backends/omm.py`, `apps/pbx/dialplan.py` |
| 2.4b | Per-extension features: **call forwarding** (always/delayed/busy/unanswered to another extension), **custom ringback tone** (upload → 8 kHz WAV → MOH class), call waiting, caller-ID display mode, DECT encryption, announcement language | ✅ | `apps/extensions`, `apps/pbx/dialplan.py` |
| 2.5 | DECT dashboard, handset list, coverage map, site survey, alerting (webhook/ntfy/email) | ✅ | `apps/dect` (`/e/<slug>/dect/`) |
| 3.1 | Phonebook with PDF/CSV/vCard/LDIF export, **per-number business card** (vCard, QR, printable) | ✅ | `apps/phonebook` (`/e/<slug>/phonebook/<n>.vcf`, `/<n>/qr.png`, `/<n>/card/`) |
| 3.1b | **Remote phonebook for desk phones & DECT OMM**: vendor XML directories (Snom, Yealink, Grandstream, Cisco, Mitel OMM, generic) behind a per-event directory token, served to autoprovisioned phones via their own token; **read-only LDAP v3 server** (`manage.py pet_ldap`, compose service `ldap`, no external deps) | ✅ | `apps/phonebook/remote.py`, `apps/phonebook/ldap/`, `/e/<slug>/phonebook/remote/<token>/<vendor>.xml`, `/prov/<token>/phonebook.xml`, `pet phonebook directory` |
| 3.2 | Call groups (ring-all / round-robin / longest-idle, `*71`/`*72`), group admins, invitations, per-member ring delays, shortcode caller-ID prefix, nested groups | ✅ | `apps/callgroups` |
| 3.3 | Voicemail with web playback, e-mail delivery, MWI | ✅ | `apps/voicemail` |
| 3.4 | Messaging (handset SMS via OMM, web gateway, broadcasts) | 🧪 flag `messaging` | `apps/messaging` |
| 3.5 | Announcements, IVR menus, fun services (echo, time, MoH) | 🧪 flag `ivr` | `apps/ivr` |
| 3.6 | Conference rooms with PIN and participant view | 🧪 flag `conferences` | `apps/conferences` |
| 3.7 | Federation between PET servers ("PET-VPN"), directory, SIP-TLS/SRTP | 🧪 flag `federation` | `apps/federation` |
| 3.8 | PSTN breakout trunks, rules, quotas, caller-ID mapping | 🧪 flag `breakout` | `apps/breakout` |
| 3.9 | Emergency targets, priority, broadcast announcement | 🧪 flag `emergency` (on by default) | `apps/emergency` |
| 3.10 | Stats / CDR dashboard, erlang per RFP, privacy controls, retention, GDPR export/delete | ✅ | `apps/stats` |
| 3.11 | Guest extensions claimable by QR, auto-expiry | ✅ flag `guest_extensions` | `apps/extensions/services.py`, `/e/<slug>/orga/guests/` |
| 3.12 | Live availability checker, waitlist, extension transfer | ✅ | `/api/v1/availability/`, `apps/extensions` |
| 3.13 | REST API + OpenAPI, webhooks (HMAC), CLI, service tokens | ✅ | `apps/api`, `apps/events/webhooks.py`, `apps/api/cli.py` |
| 3.14 | Roles: admin / orga / helpdesk / user, audit-logged; e-mail-confirmed signup (`PET_REQUIRE_EMAIL_VERIFICATION`), verified-address badge, confirmed e-mail changes; **helpdesk number history** (who had a number before, across visible events) | ✅ | `apps/events.EventMembership`, `apps/core.AuditLog`, `apps/accounts` (`RegistrationEmailToken`), `apps/extensions/history.py` (`/api/v1/extensions/history/`) |
| 3.14b | **Single sign-on via OpenID Connect** (Keycloak, Authentik, Zitadel, …): authorization code + PKCE, account linking by verified e-mail, auto-create, optional SSO-only mode, link/unlink on the profile, `manage.py pet_oidc_check` | ✅ `PET_OIDC_*` | `apps/accounts/oidc.py`, `apps/accounts/oidc_views.py` (`/accounts/oidc/login/`, `/accounts/oidc/callback/`) |
| 3.15 | Offline-friendly: Compose, Ansible, JSON export/import | ✅ | `docker-compose.yml`, `deploy/ansible`, `/api/v1/events/<slug>/export/` |
| 3.15b | **Venue agent**: PBX configuration sync to the venue over HTTPS only (snapshots with ETag, local PostgreSQL for Asterisk Realtime, heartbeats with registration state, offline-tolerant) as an alternative to the shared database; per-event provisioning mode, agent status card, `pbx.snapshot.changed` webhook | ✅ | `apps/pbx/snapshot.py`, `/api/v1/pbx/snapshot/`, `/api/v1/pbx/agent/heartbeat/`, `deploy/venue-agent/`, `deploy/asterisk/docker-compose.venue.example.yml`, `pet pbx agent status` |
| 4 | Django 5.2 + DRF, PostgreSQL, Redis + Celery, adapter pattern (PBX/DECT), **durable PBX outbox** (`PBXJob`, retries/backoff, dashboard widget), server-rendered UI with dark mode (English only), argon2, rate limiting, tests + CI | ✅ | `pet/`, `apps/pbx/base.py`, `apps/pbx/outbox.py`, `apps/dect/base.py`, `.github/workflows/ci.yml` |
| 4b | **Installable PWA** (manifest, service worker, offline page - read-only pages survive a dropped uplink; `PET_PWA_ENABLED`), **accessibility**: WCAG-AA contrast in both themes, keyboard/screen-reader landmarks, reduced-motion / high-contrast / touch-target media queries, no inline handlers, and an **automated a11y checker** run against every page in the test suite (`apps/core/a11y.py`, `manage.py pet_a11y`) | ✅ | `apps/core/views.py`, `static/js/sw.js`, `static/js/pwa.js`, `static/css/pet.css`, `apps/core/tests/test_a11y.py` |
| 5 | Architecture doc, operator handbook, user guide, API docs, OpenAPI | ✅ | `docs/` |

## Quickstart (Docker Compose)

```sh
cp .env.example .env          # edit SECRET_KEY etc. for anything but a demo
docker compose up --build     # PET web + worker + beat + ldap, PostgreSQL, Redis, Asterisk
```

The compose file shares three volumes between PET and Asterisk: `media` (ringback tones), `voicemail`
and `recordings` (`PET_RECORDING_DIR`, announcements recorded by phone). Keep them shared when you split
the stack across machines. The `ldap` service is the read-only LDAP phonebook for desk phones and the
DECT OMM (port `3890`, `PET_LDAP_PORT`); drop it if no phone at your venue speaks LDAP - the XML
directories are served by `web`. Optional single sign-on is configured with the `PET_OIDC_*` variables in
`.env` (see [Operator Handbook §3](docs/OPERATOR_HANDBOOK.md)).

Open <http://localhost:8000>. The demo event `demo` ("Demo Camp") is seeded automatically
(`PET_SEED_DEMO=1`). For a real server also run the (idempotent) seed commands once:
`manage.py pet_provisioning_profiles` (builtin Snom/Yealink/Grandstream/Cisco autoprovisioning
templates), `manage.py pet_dect_vendors` (DECT manufacturer codes, best-effort list) and, if you don't
run Celery beat, `manage.py pet_purge_tokens` from cron to prune expired e-mail confirmation tokens.

| Login | Password | Role |
|---|---|---|
| `admin@pet.local` | `admin` | global admin |
| `orga@pet.local` | `demo1234!` | orga of `demo` |
| `alice@pet.local`, `bob@pet.local`, `carol@pet.local`, `dave@pet.local` | `demo1234!` | users |

What to click first:

- `/e/demo/` - event dashboard, your extensions, phonebook, callbacks, voicemail
- `/e/demo/orga/` - orga dashboard: number plan, moderation queue (8001/8002/8003/7777 are waiting), members, audit log, export, PBX outbox widget
- `/e/demo/numbering/pools/`, `/e/demo/numbering/claims/` - extension pools for random numbers, reserve a number for a person
- `/e/demo/dect/` - DECT dashboard with RFPs, handsets, coverage map ("Demo Field"), alerts
- `/api/docs/` - Swagger UI for the REST API (`/api/redoc/` for ReDoc)
- `/docs/` - this documentation, rendered inside the portal (searchable, with table of contents)

Register a softphone against Asterisk (SIP `localhost:5060`, credentials on any SIP device page) and dial
`9003` (echo), `9000` (test ringback), `9001` (wake-up) or `4242`. See
[`deploy/asterisk/README.md`](deploy/asterisk/README.md) for the call flow. A venue that cannot (or
should not) open PET's database runs the same Asterisk image next to a local PostgreSQL fed by the
**venue agent** - [`deploy/venue-agent/README.md`](deploy/venue-agent/README.md).

## Local development

```sh
make dev          # venv + dependencies + .env
make migrate
make seed         # demo event, users, extensions, RFPs
make run          # http://localhost:8000 (sqlite, Celery eager, dummy PBX/DECT)
make test         # pytest, sqlite in-memory
make lint         # ruff
make openapi      # docs/api/openapi.yaml
```

`make worker` / `make beat` start Celery when `CELERY_TASK_ALWAYS_EAGER=0`. In eager mode the PBX
outbox delivers synchronously (override with `PET_PBX_OUTBOX_SYNC=true|false` in `.env`). Additional seed
commands: `python manage.py pet_provisioning_profiles`, `python manage.py pet_dect_vendors`.

## Repository layout

```
pet/              settings (base/dev/prod/test), urls, celery
apps/core         audit log, feature flags, rate-limit middleware, seed_demo / pet_token commands
apps/accounts     User (e-mail login, OpenID Connect SSO), e-mail confirmation tokens, ServiceAccount tokens, GDPR export/delete
apps/events       Event (lifecycle + scheduled transitions), EventMembership (roles), UserGroup, Webhook
apps/numbering    NumberPlan / NumberRange policy engine, prefix-free rule, trunk blocks, ExtensionPool, ExtensionClaim
apps/extensions   Extension lifecycle services, forwarding (+ feature codes), ringback tones, waitlist, transfer, porting, guests, CSV import, number history
apps/devices      Device, DeviceBinding, ProvisioningProfile (+ /prov/ server, softphone XML, phonebook.xml), DECTManufacturer, GSM, endpoint type registry, QR
apps/pbx          PBXAdapter, Asterisk backend (realtime tables + ARI/AMI), durable outbox (PBXJob), hook/route API, venue-agent snapshots
apps/dect         DECTAdapter, Mitel OMM backend, RFP/cluster/map/alert models, monitoring
apps/callback     CCBS/CCNR, test ringback, wake-up calls
apps/pages        per-event info pages (orga-written Markdown)
apps/phonebook    phonebook + exports, remote XML directories for desk phones / OMM, LDAP v3 phonebook server (ldap/)
apps/callgroups, voicemail, stats                      implemented Section-3 features
apps/messaging, ivr, conferences, federation, breakout, emergency   flag-gated features
apps/api          API root, auth, permissions, core viewsets, CLI (`pet`)
apps/portal       server-rendered self-service + orga UI
deploy/asterisk   reference Asterisk 20 container      deploy/ansible   playbook
deploy/venue-agent  venue agent (snapshot sync into a local PostgreSQL, Dockerfile, systemd unit, tests)
docs/             documentation (see index below)      tests/integration live Asterisk tests
```

## Adapters

PET never talks to hardware directly; it goes through two adapter interfaces selected by environment
variables (dotted class paths):

| Variable | Default | Options |
|---|---|---|
| `PET_PBX_BACKEND` | `apps.pbx.backends.dummy.DummyPBX` | `apps.pbx.backends.asterisk.AsteriskPBX` (compose default) |
| `PET_DECT_BACKEND` | `apps.dect.backends.dummy.DummyDECT` | `apps.dect.backends.omm.MitelOMM` (needs `OMM_HOST`, `OMM_USER`, `OMM_PASSWORD`) |

The dummies are in-memory and used in dev/tests, so the whole portal works on a laptop without a PBX.
Optional features are toggled with `PET_FEATURES` (see `.env.example`). Adding a FreeSWITCH or another DECT
backend means implementing `apps/pbx/base.py::PBXAdapter` or `apps/dect/base.py::DECTAdapter` - see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#adapter-pattern).

## Documentation

Every guide below is also served **inside the portal at `/docs/`** (top bar → *Docs*): rendered from the
same Markdown files with a table of contents, anchors, search across all guides and Mermaid diagrams
(diagrams need the jsDelivr CDN; offline they show as text). Edit the `.md` files - the pages update on the
next request (`apps/portal/docs.py`).

| Document | Audience |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | data model, diagrams, design decisions |
| [`docs/OPERATOR_HANDBOOK.md`](docs/OPERATOR_HANDBOOK.md) | "Running PET at your event" - orga / NOC |
| [`docs/EVENT_GUIDE.md`](docs/EVENT_GUIDE.md) | admins & orga: creating an event, roles per event, number plan, lifecycle, clone/export |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | attendees: numbers, handsets, softphones, services |
| [`docs/API.md`](docs/API.md) + [`docs/api/openapi.yaml`](docs/api/openapi.yaml) | integrators: REST API, webhooks, CLI |
| [`docs/DEVELOPING.md`](docs/DEVELOPING.md) | contributors: layout, contracts, testing |
| [`CHANGELOG.md`](CHANGELOG.md) | what changed between releases |
| [`deploy/asterisk/README.md`](deploy/asterisk/README.md) | reference PBX: realtime tables, dialplan, hooks, shared database vs. venue agent |
| [`deploy/venue-agent/README.md`](deploy/venue-agent/README.md) | venue agent: PBX config sync over HTTPS into a local database, offline behaviour |
| [`deploy/ansible/README.md`](deploy/ansible/README.md) | Ansible deployment |

## License & acknowledgements

PET is licensed under the GNU Affero General Public License v3.0 or later - see [`LICENSE`](LICENSE).

PET is inspired by the work of [Eventphone](https://eventphone.de/) - GURU3, the EPVPN federation and
years of running DECT networks at Chaos Communication Congress and CCCamp. PET is an independent project
and is **not affiliated with or endorsed by** Eventphone or the CCC. Mitel, SIP-DECT, Snom, Yealink,
Grandstream and Cisco are trademarks of their respective owners.
