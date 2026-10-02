# Developing DIAL

This document describes the project layout, conventions and integration
contracts between the DIAL apps. Read it before adding code.

## Toolchain

```sh
.venv/bin/python manage.py <cmd>              # Django (settings: dial.settings.dev)
.venv/bin/python -m pytest apps/<app> -p no:cacheprovider   # tests (settings: dial.settings.test, sqlite in-memory)
.venv/bin/python manage.py makemigrations <app>
.venv/bin/ruff check apps
```

Python 3.12+, Django 5.2, DRF, Celery (eager in dev/test), PostgreSQL in prod, sqlite for tests.

## Layout

```
dial/                 settings (base/dev/prod/test), urls, celery
apps/core            TimeStampedModel, AuditLog + audit.log(), feature flags, middleware
apps/accounts        User (email login, `username` = public nickname, `oidc_subject`), ServiceAccount (API tokens), oidc.py / oidc_views.py (OpenID Connect login, PKCE, no extra deps)
apps/events          Event (multi-tenant root, lifecycle schedule), UserGroup, EventMembership (roles), Webhook + webhooks.emit()
apps/numbering       NumberPlan / NumberRange policy engine (PolicyResult), blocks.py (trunk number blocks)
apps/extensions      Extension, ExtensionType, services.* (register/approve/reject/transfer/port/guest), csv_import, history, feature_codes, tasks
apps/devices         Device (DECT/SIP/...), DeviceBinding (multi-device), ProvisioningProfile, endpoint_types, softphone (QR provisioning XML), prov_views (/prov/ incl. phonebook.xml)
apps/pages           InfoPage (orga-written Markdown pages per event), rendering.py (escaped Markdown)
apps/pbx             PBXAdapter interface (base.py), backends/{dummy,asterisk}, realtime tables, PBX hooks API, snapshot.py (venue-agent snapshots + heartbeats)
apps/dect            DECTAdapter interface (base.py), models (RFP, SyncCluster, VenueMap, Alert...), OMM backend, monitoring
apps/callback        CCBS/CCNR, test ringback, wake-up calls
apps/phonebook       phonebook + exports, remote.py (vendor XML directories for desk phones / OMM), ldap/ (ber, protocol, directory, server: read-only LDAP v3 server)
apps/callgroups, voicemail, stats        Section-3 features (fully implemented)
apps/messaging, ivr, conferences, federation, breakout, emergency   Section-3 features (behind flags)
apps/api             REST root (auto-discovery), auth, permissions, core API, CLI
apps/portal          server-rendered self-service + orga UI
deploy/venue-agent   dial_venue_agent.py (stdlib + psycopg, runs at the venue - not a Django app), Dockerfile, systemd unit, test_agent.py
templates/base.html  shared layout; static/css/dial.css, static/js/dial.js
```

Management commands worth knowing besides the seeds (`seed_demo`, `dial_provisioning_profiles [--update]`,
`dial_dect_vendors`, `dial_purge_tokens`): `dial_ldap [--host H --port P --cert pem]` runs the LDAP
phonebook server (compose service `ldap`), `dial_oidc_check` validates the `DIAL_OIDC_*` settings against
the provider's discovery document, `pbx_venue_schema [--out file]` prints the PostgreSQL DDL a venue
agent needs (`apps.pbx.snapshot.venue_schema_sql()`), `dial_a11y [--url U] [--all]` runs the accessibility
linter against the seeded demo pages (see *Accessibility conventions* below).

## Key model facts

- `Event.state`: draft → registration → live → archived (`event.transition()`); `event.registration_open`.
  Optional schedule `registration_opens_at` / `goes_live_at` / `archives_at` (`validate_schedule()`,
  `event.next_scheduled_transition()`), applied by `apps.events.tasks.apply_scheduled_transitions` (beat,
  60 s, key `events-apply-scheduled-transitions`) one lifecycle step at a time; timestamps are cleared
  after use. `clone()` does not copy them.
- `User.role_for(event)` → `"user"|"helpdesk"|"orga"|"admin"|None`; `user.is_orga(event)`, `user.is_helpdesk(event)`,
  `user.groups_for(event)` (group slugs). Superusers are orga everywhere.
- `Extension`: `event`, `number`, `type` (ExtensionType), `owner`, `state` (requested/active/suspended/rejected/expired/deleted),
  `display_name`, `in_phonebook`, `location_hint`, `ring_strategy`, `forward_*`, `allow_callback`, `priority`, `config` (JSON, type-specific).
  `ext.bindings` → DeviceBinding → `device`. `ext.caller_id_name`, `ext.sip_uri`.
  Never change state directly - use `apps.extensions.services`.
  Trunks (`ExtensionType.TRUNK`): `config["block_digits"]` ∈ {1,2,3}, base ends in that many zeros;
  `ext.is_trunk`, `ext.block_digits`, `ext.block_range()`, `ext.covers(number)`, `ext.number_label`
  (`4700–4799`); policy via `apps.numbering.blocks.evaluate_block` (always `requires_approval`),
  overlap both ways via `services.taken_info(..., block_digits=)`; exactly one SIP device.
  `ext.announcement_record_code` / `ext.issue_record_code()` - one-time code for recording an IVR
  announcement by phone (`apps.ivr.services.begin_phone_recording` / `finish_phone_recording`).
  `apps.extensions.history.number_history(event, number, viewer)` - every extension that carried a
  number (other events only for superusers / staff there).
  `apps.extensions.csv_import.preview()` / `apply()` - bulk import (`COLUMNS`, per-row atomic,
  `create_users` sends `apps.accounts.tokens.send_invitation_mail`).
- `Device`: `type` (dect/sip/webrtc/gsm/analog), `ipei`, `subscription_pin`, `omm_ppn`, `last_seen_rfp`, `sip_username`,
  `sip_password`, `sip_transport`, `state`. `device.ensure_sip_credentials()`, `device.issue_subscription_pin()`,
  `device.softphone_links()` (`generic` / `linphone` / `acrobits`; served by `/prov/<token>/linphone.xml|acrobits.xml`).
- `NumberPlan` (one per event): `min_length/max_length`, service numbers (`test_ringback_number`,
  `wakeup_service_number`, `site_survey_number`, `echo_test_number`, `voicemail_number`, `dect_claim_number`,
  `announcement_record_number`), `emergency_numbers`
  (list), feature codes (`callback_request_code` *66, `callback_cancel_code` *86, `group_login_code` *71,
  `group_logout_code` *72, `forward_set_code` *21, `forward_clear_code` *20, `forward_busy_code` *22,
  `forward_noanswer_code` *23; blank = off). `plan.evaluate(number, user=..., extension_type=...)` → `PolicyResult`;
  `plan.reserved_numbers()` = digit-only service numbers + emergency numbers + feature codes.
  Get it with `apps.extensions.services.get_plan(event)`.
- `apps.pages.models.InfoPage`: `event`, `slug`, `title`, `body` (Markdown), `order`, `published`,
  `show_on_dashboard`, `updated_by`. Render bodies only through `apps.pages.rendering.render_markdown()`
  (escapes HTML first, drops `javascript:` links; no blockquotes) - never with the docs renderer.
- `apps.dect.models.RFP`: `omm_id`, `name`, `connected`, `synced`, `cluster`, `pos_x/pos_y`, `venue_map`;
  `SyncCluster.health`; `Alert`; `RFPStatusSample`; `SiteSurveyLog`.
- `apps.phonebook.models.PhonebookSettings` (`services.get_settings(event)`): `directory_token`,
  `directory_enabled`, `rotate_directory_token()`. `remote.token_ok(settings, token)` is the constant-time
  check; `remote.directory_info(event)` returns `{enabled, token, urls, ldap}` for UI/API/CLI;
  `remote.render_directory(event, vendor, q=)` the XML. The token is excluded from `export.py` on purpose.
- `apps.pbx.models.PBXConnection`: `provisioning` (`shared_db` | `agent`), `agent_poll_interval`,
  `agent_last_seen/version/host/software/asterisk_ok/message`, properties `is_agent`, `agent_is_stale`
  (3× poll interval), `agent_behind`. `apps.pbx.snapshot`: `build_snapshot(event)` (tables + content
  hash `version`), `snapshot_version(event)`, `venue_schema_sql()`, `apply_heartbeat(event, payload)`
  (writes `agent_*` without bumping `updated_at`, replaces the event's `ps_contacts`), `agent_state(conn)`,
  `notify_if_changed(event)` (webhook `pbx.snapshot.changed`, agent mode only - called from outbox
  delivery). Row scoping: endpoints by `accountcode`/`context`, the rest via endpoint ids / context.
- `apps.accounts.oidc`: `enabled()`, `password_login_allowed()`, `template_context()`, `start_flow()`,
  `exchange_code()`, `claims_from_tokens()`, `resolve_user(claims)` → `(user, "existing"|"linked"|"created")`,
  `link_user()`. `User.oidc_subject` = `"<issuer>|<sub>"`. Discovery is cached 1 h under `oidc:discovery:*`.

## Cross-app helpers

- Audit: `from apps.core.audit import log; log(action="approve", actor=user, target=obj, event=ev, message="...", changes={...}, request=request)`.
- Feature flags: `apps.core.features.enabled("voicemail", event)` / `@require("voicemail")` decorator. Flags:
  phonebook, callgroups, voicemail, stats, messaging, ivr, conferences, federation, breakout, emergency,
  guest_extensions, waitlist, webhooks. Template context has `DIAL_FEATURES` dict.
- Webhooks: `from apps.events.webhooks import emit; emit("extension.approved", payload_dict, event=ev)`.
- Feature codes dialled from handsets: the `feature-code` hook walks `FEATURE_CODE_MODULES` in
  `apps/pbx/api.py` (`apps.callback.services`, `apps.callgroups.services`, `apps.extensions.feature_codes`)
  and calls `handle_feature_code(event, caller, code, target) -> bool` on each until one handles it. To add a
  code: put the code string on `NumberPlan`, write the handler, append the module to the tuple.
- Adapters: `from apps.pbx import get_pbx; get_pbx(event).originate(...)`, `from apps.dect import get_dect`.
  **Always pass the event** when you have one - it selects the event's venue PBX/OMM (`PBXConnection` /
  `DECTConnection`); `get_pbx()` without an event is the server default and only right for
  event-less code paths.
- Settings: `settings.ASTERISK` dict, `settings.OMM` dict, `settings.ALERTING` dict, `settings.DIAL_*`.
  Add new settings only via `getattr(settings, "DIAL_X", default)`; do not edit `dial/settings/*`.

## Adding UI (portal) to an app

Create `apps/<app>/urls.py`:

```python
from django.urls import path
from . import views
app_name = "<app_label>"
PORTAL_MOUNT = True            # mounts at /e/<slug:slug>/<app_label>/
urlpatterns = [path("", views.index, name="index"), ...]
```

Every view receives `slug` (event slug). Use the helper:

```python
from apps.portal.shortcuts import get_event_or_404, require_orga, require_helpdesk, with_event
```

`@with_event` / `@require_orga` / `@require_helpdesk` wrap `def view(request, slug, *, event, ...)`.

Templates go in `apps/<app>/templates/<app>/*.html`, extend `base.html`, define `{% block title %}` and
`{% block content %}`. Available CSS: `.container .card .grid
.grid-2 .page-header .actions .btn .btn-primary .btn-danger .btn-ok .btn-sm .badge .badge-<state> .alert
.alert-<level> .table-wrap table .kv .tabs .number .muted .small .stat .stat-label .dot .dot-ok/.dot-err`.
Add `data-refresh="15"` to a wrapper element for auto-reload on live dashboards.
The base nav links to `phonebook:index`, `callgroups:index`, `callback:index`, `voicemail:index` (all with `slug`).

### Language

The web UI, e-mails, CLI and API are **English only** - there are no translation catalogs, no `locale/`
directory, no language switcher and no per-user language. Write UI text in plain English using DIAL's
vocabulary (extension, handset, claim, call group, orga, helpdesk); do not add `makemessages` /
`compilemessages` steps. The existing `{% trans %}` / `gettext_lazy` markup in templates and models is
harmless Django convention and may stay or be omitted in new code - it is not maintained as a translation
surface.

The only language setting that exists is the **PBX announcement language** (Asterisk sound pack) - event
default `Event.default_language` plus the per-extension override `Extension.language`, both limited to
`settings.DIAL_PBX_LANGUAGES` (`en`, `de`). It ends up in `Set(CHANNEL(language)=…)` and
`ps_endpoints.language`, never in the web UI.

### Accessibility conventions (checked by tests)

Every page in the smoke-test URL list is rendered as anonymous / user / orga / admin in
`apps/core/tests/test_a11y.py` and must produce **zero** findings from `apps.core.a11y.check_html()` - a
small `html.parser`-based linter (rules: `img-alt`, `input-label`, `button-name`, `link-text`,
`heading-order`, `landmarks`, `table-headers`, `lang`, `no-onclick`, `aria-hidden-focusable`,
`duplicate-id`, `positive-tabindex`, `iframe-title`, `role-on-div-needs-tabindex`, `details-summary`;
the module docstring explains each). There is no allow-list: fix the template. To get a new page checked,
add its URL to the lists in `scripts/smoke.py` (they are shared via `a11y.smoke_page_lists()`). During
development `manage.py dial_a11y --url /e/demo/orga/` (or `--all`) prints findings for the seeded demo DB.

What that means in practice:

- **No inline handlers.** Use the delegated actions in `static/js/dial.js`:
  `<button data-action="print">`, `<button data-action="copy" data-target="#id" data-copied="{% trans 'Copied' %}">`,
  `<select data-action="submit">` (submit the enclosing form on change),
  `<select data-action="navigate" data-href="/x/__slug__/">`. JS never contains user-facing strings - pass
  them through `data-` attributes.
- **Forms** go through `{% include "portal/_form.html" with form=form %}` (or `portal/_field.html` per
  field): it emits `<label for>`, links help text and errors with `aria-describedby`, sets
  `aria-invalid="true"` on errored widgets and marks required fields. Hand-written inputs need a
  `<label>` or `aria-label`.
- **One `<h1>` per page**, no skipped heading levels; icon-only buttons get `aria-label`; decorative glyphs
  get `aria-hidden="true"`; every `<table>` has `<th>`; every `<details>` a `<summary>`.
- **Status messages**: Django messages render with `role="status"` (success/info) or `role="alert"`
  (error); live regions you add yourself get `aria-live="polite"`.
- **Colour is never the only signal**: `.badge-ok/-warn/-err` carry ✓ / ! / ✕ via CSS `::before`; if you
  add a badge class, extend that rule in `dial.css` (section *badges*).
- CSS already handles `prefers-reduced-motion`, `prefers-contrast: more`, 44 px touch targets under
  `(pointer: coarse)` and horizontal table scrolling (`dial.css` section 11b) - don't override them. When
  you change a theme colour, keep text/muted/badge contrast ≥ 4.5:1 in both themes (a 10-line WCAG
  luminance script against the `:root` / `[data-theme="light"]` variables is enough to check).

### PWA assets

`/manifest.webmanifest`, `/sw.js` and `/offline/` are Django views in `apps/core/views.py`; the service
worker source is `static/js/sw.js` (rendered with the asset version and precache list baked in - the raw
static file is inert). The cache name is `ASSET_VERSION`, computed from the mtimes of the files in
`VERSIONED_ASSETS` (`apps/core/context_processors.py`): **add any new CSS/JS file there**, otherwise
installed clients keep serving the old copy. Cached HTML pages are limited to an allowlist in `sw.js`;
extend it only for read-only, non-personal pages and never for anything carrying secrets. Icons come from
`scripts/make_icons.py` (Pillow, no font needed).

## Adding API to an app

Create `apps/<app>/api.py` with `def register(router)` (DRF viewsets) and/or `urlpatterns` (function views).
It is auto-mounted under `/api/v1/`. Use permissions from `apps.api.permissions` (`HasScope`, `IsEventOrga`,
`IsOwnerOrOrga`) and declare `required_scopes = {"get": ["<app>:read"], "default": ["<app>:write"]}`.

## PBX ↔ DIAL contracts

The PBX (Asterisk) talks back to DIAL through authenticated hook endpoints exposed by `apps/pbx/api.py`
(`POST /api/v1/pbx/hooks/<kind>/`, header `X-DIAL-PBX-Secret`). The PBX package dispatches into:

| Hook kind        | Called into                                                                   |
|------------------|--------------------------------------------------------------------------------|
| `feature-code`   | `apps.callback.services.handle_feature_code(event, caller, code, target)`, `apps.callgroups.services.handle_feature_code(...)` and `apps.extensions.feature_codes.handle_feature_code(...)` (`*21<n>`/`*22<n>`/`*23<n>` set forwarding, `*20` clears) - modules from `FEATURE_CODE_MODULES`, each returns `True` if handled |
| `extension-idle` | `apps.callback.services.on_extension_idle(event, number)` (busy ext became free → fire CCBS) |
| `cdr`            | `apps.stats.services.ingest_cdr(event, record: dict)` (`record` keys: src, dst, start, answer, end, duration, billsec, disposition, channel, dstchannel, uniqueid, rfp) |
| `voicemail`      | `apps.voicemail.services.store_message(event, mailbox_number, caller, file_path, duration)` |
| `site-survey`    | `apps.dect.services.log_site_survey(event, caller_number)` → returns RFP name to announce |
| `dect-claim`     | `apps.dect.claim.claim_handset(event, caller, code, callerid)` → `{handled, number}` |
| `announcement-record-start` | `apps.ivr.services.begin_phone_recording(event, caller, code, callerid)` → `{handled, number, name, file}` (`file` = absolute path without extension inside `DIAL_RECORDING_DIR`, `name` = its basename) |
| `announcement-recorded` | `apps.ivr.services.finish_phone_recording(event, code, file, duration)` → `{handled, number}`; imports `<file>.wav` into `MEDIA_ROOT/ivr/<slug>/<number>/` when readable, emits `announcement.recorded` |

The PBX asks DIAL for routing information via `GET /api/v1/pbx/route/?event=<slug>&number=<n>` which uses:

- `apps.callgroups.services.dial_targets(extension) -> list[str]` (numbers of logged-in members + strategy)
- `apps.ivr.services.dialplan_for(extension) -> dict`
- `apps.emergency.services.route(event, number) -> str | None`
- `apps.breakout.services.authorize(event, extension, destination) -> (bool, reason)`
- `apps.federation.services.route(event, number) -> str | None`

Each of these must exist and degrade gracefully (return empty/None) when the feature flag is off.

**Venue agent contract** (`apps/pbx/api.py`, auth: hook secret `X-DIAL-PBX-Secret`, service token with
scope `pbx:sync`, or orga session):

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/pbx/snapshot/?event=` | `{event, version, generated_at, poll_interval, tables: {name: {key, rows}}}`; honours `If-None-Match` and answers `304` with the same `ETag` while the version is unchanged |
| `GET /api/v1/pbx/snapshot/schema/?event=` | `{dialect: "postgresql", sql, tables}` - `CREATE TABLE IF NOT EXISTS` DDL from `SCHEMA_MODELS` |
| `POST /api/v1/pbx/agent/heartbeat/` | body `{event, version, hostname, agent_version, asterisk_ok, message, contacts?}`; answers `{ok, current_version, poll_interval, behind, connection}`; `contacts` replace DIAL's `ps_contacts` for the event's endpoints |

Adding a table to the snapshot means adding it to `SNAPSHOT_TABLES` (model + key column) and, if it must
exist at the venue, to `SCHEMA_MODELS`; the agent maps unknown columns away with a warning, so old agents
keep working with newer snapshots.

## Testing

Fixtures in `conftest.py`: `user`, `other_user`, `admin`, `event` (slug `demo`, 4-digit plan with ranges),
`angels` (UserGroup), `orga`, `member`. Celery is eager; PBX/DECT backends are the in-memory dummies
(`get_pbx()` is `lru_cache`d and `get_pbx(event)` cached per event - call `apps.pbx.reset_pbx_cache()` /
`apps.dect.reset_dect_cache()` if you swap the backend or create a `PBXConnection` in a test; the orga
view does this for you). `DummyDECT.calls` records every subscription call as `(method, kwargs)`
(`create_subscription` / `update_subscription` / `delete_subscription`) - use it to assert e.g. that a
display-name change produced an `update_subscription` and no new `create_subscription`.

Three test suites have extra needs:

- **LDAP server tests** (`apps/phonebook/tests/test_ldap.py`) start the real asyncio server on
  `127.0.0.1` with an ephemeral port and talk to it with the tiny client in
  `apps/phonebook/ldap/protocol.py` (one test additionally shells out to `ldapsearch` when installed) -
  the sandbox or CI runner must allow listening on loopback.
- **OIDC tests** (`apps/accounts/tests/test_oidc.py`) enable SSO via `override_settings` and mock
  `requests`; `dial.settings.test` keeps `DIAL_OIDC_ENABLED=False` so other tests see the password login.
- **Venue agent tests** live outside `apps/` and are not collected by the default configuration; run them
  explicitly from the repository root:

```sh
.venv/bin/python -m pytest -p no:cacheprovider -p no:warnings -o addopts="" -q deploy/venue-agent
.venv/bin/ruff check deploy/venue-agent
```

They mock the database, HTTP and AMI, so no PostgreSQL or Asterisk is needed.

## Documentation pages (`/docs/`)

`apps/portal/docs.py` renders the Markdown files listed in its `DOCS` registry (title = first `#` heading,
cached per file mtime). Python-Markdown with `tables`, `fenced_code`, `codehilite` (Pygments, theme-aware
CSS in `dial.css` §10b), `toc` (h2–h3, `#` permalinks) plus two small tree processors: `*.md` links become
portal links, `- [ ]` becomes a checkbox. ```` ```mermaid ```` blocks pass through as `<pre class="mermaid">`
and are drawn client-side only if the CDN loads. To add a guide: drop the file in `docs/` and append a
`Doc(...)` entry (slug, path, label, audience, summary, icon).

## See also

- [`../README.md`](../README.md) - quickstart, feature matrix, repository layout
- [`ARCHITECTURE.md`](ARCHITECTURE.md) - data model, component/sequence diagrams, design decisions
- [`EVENT_GUIDE.md`](EVENT_GUIDE.md) - creating, staffing and running an event
- [`OPERATOR_HANDBOOK.md`](OPERATOR_HANDBOOK.md) - running DIAL at an event, troubleshooting
- [`USER_GUIDE.md`](USER_GUIDE.md) - attendee documentation
- [`API.md`](API.md) and [`api/openapi.yaml`](api/openapi.yaml) - REST API, webhooks, CLI
- [`../deploy/asterisk/README.md`](../deploy/asterisk/README.md) - reference PBX container
- [`../deploy/venue-agent/README.md`](../deploy/venue-agent/README.md) - venue agent (snapshot sync without a shared database)
- [`../deploy/ansible/README.md`](../deploy/ansible/README.md) - Ansible deployment
- [`../tests/integration/README.md`](../tests/integration/README.md) - live Asterisk integration tests
