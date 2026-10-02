# Changelog

All notable changes to DIAL are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); DIAL does not have tagged releases yet.

## Unreleased

### Added — early-access gate

- `DIAL_EARLY_ACCESS_PASSWORD` puts a shared password in front of every page and the API while DIAL runs
  on its public domain before launch (`/early-access/`, signed cookie bound to the current password, rate
  limited). Phones, PBX hooks, the venue agent, the remote phonebook, the federation directory and
  `dial_` service tokens are exempt. Same contract as EVAC's gate.

### Changed — renamed PET to DIAL

- The project is now **DIAL - DECT & IP Administration Layer** (formerly PET - Portable Event Telephone).
  Everything was renamed, with no compatibility aliases: the Django project package `pet/` → `dial/`
  (`DJANGO_SETTINGS_MODULE=dial.settings.*`, `celery -A dial`), every `PET_*` setting / environment variable
  → `DIAL_*`, the `X-PET-PBX-Secret` header → `X-DIAL-PBX-Secret`, service tokens `pet_…` → `dial_…`,
  `manage.py pet_*` commands → `dial_*`, the `pet` CLI → `dial`, Asterisk contexts/variables (`pet-*`,
  `PET_*`) and the default ARI app → `dial`, static assets (`dial.css`, `dial.js`, `dial-*.png`), the
  Ansible role, the venue agent (`dial-venue-agent`), Docker users, default database names and "PET-VPN" →
  "DIAL-VPN".

### Removed — German UI translation

- The web UI is **English only** from now on. The `locale/de` catalog (2178 strings), the language switcher
  in the header, the `/i18n/setlang/` route, `LocaleMiddleware` and the per-user language preference
  (`User.language`, migration `accounts.0004`) are gone; `LANGUAGES` is `[("en", "English")]`,
  `LANGUAGE_CODE` is fixed to `en` (no longer an environment variable - removed from `.env.example` and the
  Ansible defaults). Signup / profile forms lose the *Language* field, OIDC no longer reads the `locale`
  claim, e-mails render in English, `manifest.webmanifest` reports `lang: en`, the GDPR export drops
  `user.language`. Build tooling: `make messages` / `make compilemessages`, the `gettext` package and the
  `compilemessages` step in the `Dockerfile`, and `locale/**` in `pyproject.toml` package data are removed.
  Reason: textbook German for telephony vocabulary ("Nebenstelle", "Mobilteil") reads badly to the target
  audience and a bilingual UI doubled the maintenance cost of every string; the existing `{% trans %}` /
  `gettext_lazy` markup is kept as harmless Django convention.
- **Not** removed: the PBX **announcement language** (Asterisk sound pack for voicemail and system prompts).
  It is now clearly separated from the UI: `settings.DIAL_PBX_LANGUAGES` (`en`, `de`) drives
  `Event.default_language` (now with choices, label *Announcement language* and help text; migration
  `events.0004`) and `Extension.language`; the orga settings form uses these instead of `LANGUAGES`.

### Fixed

- `templates/_sidebar.html` closed its `<aside>` immediately after opening it, so the event name / state /
  date block and the whole event navigation rendered *outside* the sidebar landmark at the top of every
  event page. The block is gone: the event's state badge now sits in the top bar next to the event name
  (`.nav-event-state`, dates in its tooltip, hidden < 760 px), and the sidebar starts directly with the
  navigation groups.

### Added — PWA & accessibility

- **Installable web app.** `GET /manifest.webmanifest`, service worker `GET /sw.js` (rendered from
  `static/js/sw.js` with `ASSET_VERSION` as cache name, `Cache-Control: no-cache`) and a cacheable
  `GET /offline/` page (`apps/core/views.py`, `core:manifest` / `core:service_worker` / `core:offline`).
  Navigations are network-first (4 s timeout) → cache → offline page; only `/`, `/offline/`, `/docs/…`,
  `/e/<slug>/` and `/e/<slug>/phonebook/` are ever cached, static assets cache-first; `/accounts/`, `/admin/`,
  `/api/`, `/prov/`, `/switch/`, orga and PBX pages, QR codes, vCard/CSV/PDF/LDIF/XML/JSON exports,
  redirects, non-200 and `no-store` responses never are; a logout purges the page cache. `static/js/pwa.js`
  registers the worker (HTTPS/localhost only), shows an offline banner (`role="status"`), a "DIAL was updated -
  Reload" toast when a new worker is waiting and an "Install DIAL on this device" button when the browser
  offers `beforeinstallprompt`. Icons in `static/icons/` (`scripts/make_icons.py`), SVG favicon,
  `theme-color` follows the event's primary colour. Settings `DIAL_PWA_ENABLED` (default on; off = no manifest
  link, `/sw.js` 404 → installed workers unregister), `DIAL_PWA_THEME_COLOR`, `DIAL_PWA_BACKGROUND_COLOR`.
- **Accessibility pass + automated checker.** `apps/core/a11y.py::check_html()` - an `html.parser`-based
  linter (rules `img-alt`, `input-label`, `button-name`, `link-text`, `heading-order`, `landmarks`,
  `table-headers`, `lang`, `no-onclick`, `aria-hidden-focusable`, `duplicate-id`, `positive-tabindex`,
  `iframe-title`, `role-on-div-needs-tabindex`, `details-summary`) - runs in `apps/core/tests/test_a11y.py`
  against every smoke-test page as anonymous / user / orga / admin and must report nothing;
  `manage.py dial_a11y [--url …] [--all]` prints findings during development. UI: `templates/portal/_form.html`
  wires label / help / errors with `aria-describedby`, `aria-invalid` and required markers; flash messages
  carry `role="status"` / `role="alert"`; the live availability output is `aria-live="polite"`; status badges
  get ✓ / ! / ✕ glyphs so state is never colour-only; `dial.css` gains `@media (pointer: coarse)` 44 px touch
  targets, `prefers-reduced-motion`, `prefers-contrast: more`, horizontal table scrolling and
  `overflow-wrap: anywhere` for tokens; muted/badge colours tuned to ≥ 4.5:1 in both themes.

### Changed

- All inline `onclick` / `onchange` handlers were replaced by delegated `data-action="print|copy|submit|navigate"`
  handlers in `static/js/dial.js` (strings come from `data-` attributes, never from JS).
- `ASSET_VERSION` now covers `pwa.css`, `pwa.js` and `sw.js` (`VERSIONED_ASSETS` in
  `apps/core/context_processors.py`).

### Documentation

- User Guide §14 *Install DIAL on your phone*, §15 *Accessibility*; Operator Handbook *Progressive web app*
  + `DIAL_PWA_*` in the configuration reference; Developing *Accessibility conventions* and *PWA assets*;
  Architecture components / security / limitations; API portal-only endpoints; README feature row;
  `.env.example`.

### Known limitations

- No Web Push and no background sync; iOS installs manually via *Add to Home Screen*; offline mode is
  read-only and limited to pages already visited.
- The a11y checker is a heuristic subset of WCAG (structure, names, handlers) - colour contrast, focus order
  and custom-widget semantics were tuned by hand and are not enforced by the tests.

### Added — remote phonebook, SSO & venue agent

- **Remote phonebook directory for desk phones, the DECT OMM and LDAP clients.** `PhonebookSettings` gained
  `directory_token` (per-event secret, `secrets.token_urlsafe(24)`) and `directory_enabled` (orga phonebook
  settings page; `directory_enabled` is part of the event export, the token never is). Portal route
  `GET /e/<slug>/phonebook/remote/<token>/<vendor>.xml` (`apps/phonebook/remote.py`; vendor `snom`
  `SnomIPPhoneDirectory`, `yealink` `YealinkIPPhoneDirectory`, `grandstream` `AddressBook/Contact`, `cisco`
  `CiscoIPPhoneDirectory`, `mitel` `IPPhoneDirectory` for the SIP-DECT OMM corporate directory, `generic`
  `IPPhoneDirectory`; `?q=` / `?search=` / `?name=` filter by name) - the token is the credential, served only for registration/live events, every
  failure is a plain `404`. `POST /e/<slug>/phonebook/settings/rotate-token/` rotates the token (audited).
  Provisioned phones fetch the same document under their own token, `GET /prov/<device token>/phonebook.xml`
  (`?vendor=` overrides the profile's vendor), so the event-wide secret never lands in a config file; the
  builtin Snom/Yealink/Grandstream/Cisco SPA templates now carry the phonebook keys (`phonebook_url` in the
  template context). REST `GET /api/v1/phonebook/directory/?event=` → `{enabled, token, urls: {vendor: url},
  ldap: {host, port, base_dn, bind_dn, password, name_attributes, number_attribute}}` and
  `POST /api/v1/phonebook/directory/rotate/?event=` (orga; scopes `phonebook:read` / `phonebook:write`). CLI
  `dial phonebook directory --event <slug> [--rotate]`. Migration `phonebook 0002`.
- **LDAP phonebook server.** `manage.py dial_ldap [--host] [--port] [--cert] [--key]` (`apps/phonebook/ldap/`:
  own BER/LDAP v3 implementation on asyncio, no external dependency) serves every registration/live event
  with the directory enabled below `dc=<slug>,dc=dial`: bind `cn=directory,dc=<slug>,dc=dial` with the event's
  directory token, search base `ou=phonebook,dc=<slug>,dc=dial`, one `inetOrgPerson` per entry
  (`cn=<name>+telephoneNumber=<number>`) with `cn`, `sn`, `givenName`, `displayName`, `telephoneNumber`,
  `mobile`, `uid`, `description`, `l`, `ou`, `o`, `title`. `--cert` switches to LDAPS. Compose service `ldap`
  (port 3890, published on `DIAL_LDAP_PORT`); settings `DIAL_LDAP_HOST`, `DIAL_LDAP_PORT`,
  `DIAL_LDAP_ALLOW_ANONYMOUS`, `DIAL_LDAP_CACHE_SECONDS`. The orga settings page, the API and the CLI print the
  values to type into a phone.
- **OpenID Connect login** (`apps/accounts/oidc.py`, `oidc_views.py`): authorization code flow with PKCE on
  plain `requests`, discovery via `<issuer>/.well-known/openid-configuration`. Routes
  `/accounts/oidc/login/` (`?next=`, `link=1` links the identity to the logged-in account),
  `/accounts/oidc/callback/`, `POST /accounts/oidc/unlink/` (refused while the account has no usable
  password); `/accounts/logout/` optionally continues to the IdP's `end_session_endpoint`. Settings
  `DIAL_OIDC_ENABLED`, `DIAL_OIDC_ISSUER`, `DIAL_OIDC_CLIENT_ID`, `DIAL_OIDC_CLIENT_SECRET`, `DIAL_OIDC_SCOPES`,
  `DIAL_OIDC_BUTTON_LABEL`, `DIAL_OIDC_AUTO_CREATE`, `DIAL_OIDC_TRUST_EMAIL_VERIFIED`,
  `DIAL_OIDC_ALLOW_PASSWORD_LOGIN`, `DIAL_OIDC_USERNAME_CLAIM`, `DIAL_OIDC_LOGOUT_AT_IDP`. Account linking:
  `User.oidc_subject == "<issuer>|<sub>"` → log in; otherwise the same e-mail address links only when the
  DIAL account is verified or the IdP asserts `email_verified` (and `DIAL_OIDC_TRUST_EMAIL_VERIFIED`); otherwise
  an account is created (`DIAL_OIDC_AUTO_CREATE`, unusable password, nickname from `DIAL_OIDC_USERNAME_CLAIM`).
  SSO-only mode (`DIAL_OIDC_ALLOW_PASSWORD_LOGIN=false`) hides the password form, signup and reset links and
  the header's *Sign up* button; password login and signup then answer `403` (superusers keep
  `/admin/login/`). Callback failures count against the login lockout. Profile page shows the linked identity
  with *Link* / *Unlink*. `manage.py dial_oidc_check` fetches the discovery document and prints endpoints,
  redirect URI and settings. SSO is browser-only: no API token exchange, service tokens are unchanged.
- **Venue agent - PBX snapshot sync** (`apps/pbx/snapshot.py`). `PBXConnection.provisioning` is `shared_db`
  (default, the venue Asterisk reads DIAL's PostgreSQL) or `agent`: a small agent next to the venue Asterisk
  polls `GET /api/v1/pbx/snapshot/?event=` → `{event, version, generated_at, poll_interval, tables:
  {ps_endpoints, ps_auths, ps_aors, ps_endpoint_id_ips, extensions, voicemail_users: {key, rows}}}` (`ETag` /
  `If-None-Match` → `304`; `version` hashes the rows minus the volatile `voicemail_users.stamp` and
  `extensions.id`), writes them into a local PostgreSQL created from `GET /api/v1/pbx/snapshot/schema/?event=`
  (`{dialect, sql, tables}`) and reports with `POST /api/v1/pbx/agent/heartbeat/` `{event, version, hostname,
  agent_version, asterisk_ok, message, contacts?}` → `{ok, event, connection, current_version, poll_interval,
  behind}` (optional `contacts` = the venue's `ps_contacts`, so registration status in DIAL keeps working).
  Auth for all three: the event's `X-DIAL-PBX-Secret`, a service token with the new scope `pbx:sync`, or an
  orga session; wrong credentials `401`. New `PBXConnection` fields `provisioning`, `agent_poll_interval`
  (min 5 s) and the heartbeat-written `agent_last_seen`, `agent_version`, `agent_host`, `agent_software`,
  `agent_asterisk_ok`, `agent_message` (read-only in the connection API / orga page) plus computed
  `agent_is_stale`, `agent_behind`, `snapshot_version` in the `connection` payload; migration `pbx 0004`.
  Webhook `pbx.snapshot.changed` `{event, version, previous}` after a PBX write in agent mode. CLI
  `dial pbx connection set --event <slug> --provisioning agent|shared_db [--agent-poll-interval N]`,
  `dial pbx agent status --event <slug>`, `dial pbx snapshot --event <slug> [--out file]`;
  `manage.py pbx_venue_schema [--out file.sql]`. Venue side: `deploy/venue-agent/dial_venue_agent.py`
  (stdlib + psycopg; env `DIAL_URL`, `DIAL_EVENT`, `DIAL_PBX_HOOK_SECRET` or `DIAL_SYNC_TOKEN`, `DATABASE_URL` /
  `DB_*`, `POLL_INTERVAL`, `ASTERISK_RELOAD` / `AMI_*`; `--once`, `--check`; reload over `asterisk -rx` or
  AMI; SIGHUP = full re-sync) with `Dockerfile`, `dial-venue-agent.service`, `README.md` and `test_agent.py`;
  `deploy/asterisk/docker-compose.venue.example.yml` (`venue-db`, `venue-agent`, `asterisk`) and
  `deploy/asterisk/.env.venue.example`.

### Changed

- `PUT`/`PATCH /api/v1/pbx/connection/` and the orga PBX form accept `provisioning` and
  `agent_poll_interval`; in agent mode an empty ARI URL means "no ARI" instead of "server default".
- `manage.py dial_provisioning_profiles` gained `--update` to rewrite existing builtin profiles to the shipped
  templates (needed once to pick up the phonebook keys).
- The header's *Sign up* button (`signup_offered` context) disappears in SSO-only mode.
- `Webhook.EVENT_TYPES` gained `pbx.snapshot.changed`; `PhonebookSettings` admin lists `directory_enabled`
  and shows the token read-only.
- `.gitignore` whitelists `.env.venue.example` next to `.env.example`.

### Documentation

- `docs/API.md`: phonebook directory endpoints, PBX snapshot / schema / heartbeat, scope `pbx:sync`, webhook
  `pbx.snapshot.changed`, `/prov/<token>/phonebook.xml`, portal-only remote XML / rotate / OIDC routes, an
  *LDAP directory* subsection, CLI and management commands. `deploy/venue-agent/README.md` (agent behaviour,
  offline mode, compose and systemd setup).

### Known limitations

- LDAP: simple bind only (no SASL), no StartTLS (use `dial_ldap --cert` for LDAPS), no paged results control;
  directory contents are cached for `DIAL_LDAP_CACHE_SECONDS` (30 s), so a new entry may take that long to
  appear on a phone.
- The `mitel` XML format is the generic `IPPhoneDirectory` schema and has not been verified on a real OMM; the
  vendor-specific provisioning keys (Snom `dkey_directory`, Grandstream `P330`–`P332`, Yealink
  `remote_phonebook.*`, Cisco `XML_Directory_Service_*`) have not been verified on real phones.
- OIDC: the ID token's signature is not verified - DIAL relies on fetching it directly from the token endpoint
  over TLS (OIDC Core §3.1.3.7) and validates `iss`, `aud`/`azp`, `exp`, `iat` and `nonce`. No back-channel
  logout, no refresh tokens, claims (name, e-mail, language) are not re-synced after the first login.
- Venue agent: calls made while the uplink is down are written to the venue's `cdr` table but never reach DIAL;
  call groups, IVR menus, feature codes, the route API and the other call-time hooks need the uplink; DIAL →
  venue ARI/AMI (originate, channel status) still needs a VPN or exposed ARI. The agent's PostgreSQL path has
  only been exercised with fakes in `test_agent.py` - no PostgreSQL was available in the sandbox.

### Added — GURU3-parity batch 2

- **SIP trunk number blocks.** New extension type `trunk` (`ExtensionType.TRUNK`) carries a whole block
  behind one SIP account: `config.block_digits` ∈ {1, 2, 3} = 10/100/1000 numbers (`4700` + 2 →
  `4700–4799`, Asterisk pattern `_47XX`). API `POST /api/v1/extensions/` with `type: "trunk"` and
  `block_digits` (write-only, fixed after registration), `block_range` (read, `["4700", "4799"]`);
  `GET /api/v1/availability/?type=trunk&block_digits=N` checks the whole block; the route API returns
  `type: "trunk"` plus `trunk: {base, range}` for the base and every number inside the block; the phonebook
  (JSON, lists, PDF) prints `number_label` (`4700–4799`). CLI `dial extensions create … --type trunk
  --block-digits N`.
- **Record announcements by phone.** `NumberPlan.announcement_record_number` (orga number plan form, API
  `events/<slug>/number-plan/`) plus a per-announcement `Extension.announcement_record_code`: the owner (or
  helpdesk) dials `<record number><code>`, Asterisk asks DIAL via `POST /api/v1/pbx/hooks/announcement-record-start/`
  (`event, caller, callerid, code` → `handled, number, name, file`), records to `file` under
  `DIAL_RECORDING_DIR` and reports back with `POST /api/v1/pbx/hooks/announcement-recorded/`
  (`event, code, file, duration`). DIAL imports the wav into media storage when it can read it, otherwise keeps
  the PBX path as playback reference; webhook `announcement.recorded`. Static `record-announcement` service
  exten in `deploy/asterisk/conf/extensions.conf`; the `recordings` volume is shared between DIAL and Asterisk in
  `docker-compose.yml` and `deploy/asterisk/docker-compose.example.yml` (`asterisk-recordings`).
- **Phone feature codes for call forwarding.** `NumberPlan.forward_set_code` (`*21`), `forward_clear_code`
  (`*20`), `forward_busy_code` (`*22`), `forward_noanswer_code` (`*23`) - editable in the number plan and
  exposed in the number-plan API; handled by `apps.extensions.feature_codes` through the `feature-code` hook.
- **CSV import of extensions and users.** `POST /api/v1/extensions/import/?event=<slug>` with
  `{csv, dry_run, create_users, allow_ownerless}` (orga; `apps/extensions/csv_import.py`): header aliases
  (`ext`, `name`, `mail`, `nick`, …), per-row plan with `action` `create|created|skip-taken|error`, response
  `{plan, applied, users_created, skipped, errors, dry_run, unknown_columns}`; missing accounts are created on
  request and receive an invitation mail; group and role columns add memberships. CLI
  `dial extensions import --event <slug> --file f.csv [--dry-run] [--create-users]`.
- **Info pages.** Orga-written Markdown pages per event (`apps.pages.InfoPage`: `slug`, `title`, `body`,
  `order`, `published`, `show_on_dashboard`, `updated_by`) under `/e/<slug>/pages/` (manage at
  `/e/<slug>/pages/manage/`), dashboard cards for flagged pages. REST `/api/v1/pages/?event=<slug>` CRUD
  (scopes `pages:read` / `pages:write`, read-only `body_html`), webhook `page.updated`, part of the event
  export/import (`pages`). CLI `dial pages list --event <slug>`, `dial pages show <slug> --event <slug>`.
- **Number history.** `GET /api/v1/extensions/history/?event=<slug>&number=<n>` (helpdesk+,
  `apps/extensions/history.py`) returns every extension that carried the number in this event and in events
  the caller staffs, their audit entries and a merged `timeline`; the helpdesk lookup page shows it when the
    query is a number.
- **Scheduled lifecycle.** `Event.registration_opens_at` / `goes_live_at` / `archives_at` (event settings form,
  API fields on `events/<slug>/` with read-only `next_scheduled_transition`), validated against the current
  state; the beat task `events-apply-scheduled-transitions` (every 60 s, `apps.events.tasks`) applies due
  schedules with an audit entry and drops stale ones. CLI `dial events schedule <slug> [--registration ISO]
  [--live ISO] [--archive ISO] [--clear]`.
- **Softphone provisioning (Linphone, Acrobits/Groundwire).** `GET /prov/<token>/linphone.xml` (lpconfig XML,
  QR `linphone-config:<url>`) and `GET /prov/<token>/acrobits.xml` (`<account>` XML, QR = URL) rendered by
  `apps/devices/softphone.py`; `softphone_links` (`generic`, `linphone`, `acrobits`) in the device detail
  serializer (`GET /api/v1/devices/<id>/`), device QR `GET /api/v1/devices/<id>/qr/?client=linphone|acrobits`
  and the matching tabs on the device page.
- **Business card.** Every phonebook entry has `vcard_url` / `card_qr_url` (API, absolute URLs) backed by the
  portal routes `/e/<slug>/phonebook/<number>.vcf`, `/e/<slug>/phonebook/<number>/qr.png` and the printable
  `/e/<slug>/phonebook/<number>/card/`; the extension page links to its card.
- Numbers are **per event**: an extension never grants a permanent claim on its number. "Porting" stays a
  re-request under the new event's number policy (`extensions/portable/` + `extensions/<id>/port/`).

### Fixed

- Changing the display name of a DECT extension updates the OMM subscription in place instead of forcing the
  handset to resubscribe (#19). `provision_dect_for_extension` pushed the number of a *secondary* binding to
  the handset; it now always pushes the primary (lowest-priority) binding, so editing a secondary extension no
  longer renumbers the handset.

### Documentation

- User Guide (trunk blocks, feature codes, recording by phone, softphone QR, business card, info pages), Event
  Guide (CSV import, scheduled lifecycle, info pages, number history), Operator Handbook (recording volume /
  `DIAL_RECORDING_DIR`, beat entry, trunk dialplan), `docs/API.md` (new endpoints, hooks, prov URLs, webhooks,
  CLI), Architecture, `DEVELOPING.md`, `README.md` and `deploy/asterisk/README.md` (record-announcement
  service, trunk routing).

### Known limitations

- Inbound calls through a trunk block present the block's base number as caller ID only.
- The `dial/record-*` voice prompts are not shipped; the dialplan falls back to Asterisk core prompts. The
  recording dialplan has not been exercised on a real Asterisk yet.
- The Linphone and Acrobits provisioning XML has not been verified on the real apps.
- Users created by the CSV import still have to verify their e-mail address before they can register
  further extensions themselves.
- Info-page Markdown does not support blockquotes.

### Added — per-event venue PBX & DECT connections

- DIAL is one central service; **every event connects its own venue infrastructure**. New models
  `apps.pbx.models.PBXConnection` (backend key, ARI URL/user/password/app, AMI host/port/user/password,
  per-event **hook secret**, notes) and `apps.dect.models.DECTConnection` (backend key, host, port, user,
  password, verify TLS, notes), one per event. Migrations `pbx 0003`, `dect 0003`.
- Orga page **`/e/<slug>/pbx/`** (*Orga · Infrastructure → PBX & DECT connection*, `apps/pbx/views.py`,
  `apps/pbx/forms.py`): status card (server default vs. configured backend), two forms, *Test connection*
  (adapter `health()`), *Reset* back to the server default, env snippet for the venue Asterisk
  (`DIAL_API_URL`, `DIAL_EVENTS`, `DIAL_PBX_HOOK_SECRET`, `SIP_DOMAIN`, hook/route/dialplan URLs). Password
  fields never echo their value; an empty submission keeps the stored secret. All changes are audited.
- `get_pbx(event)` / `get_dect(event)` return the event's adapter (instantiated with `config=` from the
  connection, cached per event until the row changes) and fall back to the server default built from
  `DIAL_PBX_BACKEND` / `DIAL_DECT_BACKEND`. Every service, outbox job, hook and view now passes its event.
  New settings `DIAL_PBX_BACKENDS` / `DIAL_DECT_BACKENDS` (adapter catalogue key → class path). All
  adapters accept `config=`.
- Hooks, route lookups, dialplan export and phone/GSM provisioning validate `X-DIAL-PBX-Secret` against
  the **event's** hook secret when set, otherwise the server-wide `DIAL_PBX_HOOK_SECRET` (event is
  resolved before authorisation; unknown event with any secret is still `400`, wrong secret `401`).
- `GET /api/v1/health/?event=<slug>` checks the event's venue adapters (`404` unknown slug); the response
  gained an `event` key. `dial health --event <slug>` in the CLI.
- REST `GET|PUT|PATCH|DELETE /api/v1/pbx/connection/?event=` (orga, `pbx:read`/`pbx:write`) mirrors the
  orga page: secrets only as `has_*` flags, partial updates keep stored secrets, same validation;
  `DELETE ?part=pbx|dect|all`. CLI `dial pbx connection show|set|reset --event <slug>`.
- Orga dashboard *Venue connection* card (highlighted while PBX or DECT still use the server default),
  link in the DECT tab bar, Django admin for `PBXConnection` / `DECTConnection` (cache reset on save).
- Tests `apps/pbx/tests/test_connections.py` (page, API, resolution) and CLI tests; smoke covers `/e/<slug>/pbx/`.

### Documentation

- Handbook §2 reframed around the deployment shape (central DIAL + venue Asterisk/OMM over VPN,
  reachability table), §7/§9 use the per-event connection, config table lists the server-default and
  catalogue settings. Event guide: new *Connect the venue infrastructure* step, checklist and
  troubleshooting rows; the word "instance" was replaced by "server". Architecture §4/§8/§11,
  API `health/?event=`, `deploy/asterisk/README.md` hook secret + env table, `.env.example` marks
  `DIAL_PBX_BACKEND`/`DIAL_DECT_BACKEND` as server defaults.

### Known limitation

- The venue Asterisk still reads realtime tables from DIAL's PostgreSQL; a venue keeps working only while
  its link to the DIAL server is up (see architecture §11).

### Added — signup/login abuse protection & forced e-mail verification

- **Spam guard** (`apps/core/antispam.py`, `SpamGuardMixin`): honeypot field + signed render timestamp on
  signup (both variants), password reset and login (honeypot only). Settings `DIAL_SPAM_GUARD`,
  `DIAL_SPAM_GUARD_MIN_SECONDS`. Honeypot fields render off-screen via `.hp` in `portal/_form.html`.
- **Login lockout** in `accounts.LoginView`: `DIAL_LOGIN_MAX_FAILURES` per account /
  `DIAL_LOGIN_IP_MAX_FAILURES` per IP within 15 min → `429` for `DIAL_LOGIN_LOCKOUT_MINUTES`, audit entry,
  `dial.security` log. Cache-backed counters (`antispam.hit/lock/locked_for/clear`), fail-open if the cache
  is down.
- **E-mail domain lists** `DIAL_SIGNUP_BLOCKED_DOMAINS` / `DIAL_SIGNUP_ALLOWED_DOMAINS`
  (`validate_signup_email`, subdomains included) on signup and e-mail change.
- `/accounts/password/reset/` added to `RateLimitMiddleware` (`DIAL_RATE_LIMITS["password_reset"]`, 10/min).
- **Unverified accounts** cannot register extensions (`extensions.services.register`, orga and
  `force_active` flows exempt) and see a banner with a resend button on every page (`base.html`).

### Changed

- `DIAL_REQUIRE_EMAIL_VERIFICATION` now defaults to **`1`** (e-mail-first signup). Set it to `0` in `.env`
  to keep the one-step signup with a follow-up verification mail.
- **Event creation is strictly global-admin.** Cloning (`portal:orga_event_clone`,
  `POST /api/v1/events/<slug>/clone/`) and `DELETE /api/v1/events/<slug>/` now require `is_superuser`,
  like `/events/new/`, `POST /api/v1/events/` and import already did. Clone buttons are hidden for orga.

### Documentation

- **In-app documentation at `/docs/`** (`apps/portal/docs.py`, templates `portal/docs/`): all guides and the
  changelog rendered from Markdown inside the portal - index with cards and full-text search, per-page
  table of contents with scroll tracking, heading anchors, code highlighting, task lists, Mermaid diagrams
  (CDN, text fallback offline). *Docs* link in the top bar and footer. New dependencies: `Markdown`,
  `Pygments`.
- New [`docs/EVENT_GUIDE.md`](docs/EVENT_GUIDE.md): roles and who-may-do-what, creating an event
  (admin only), handing out orga/helpdesk roles per event, settings, number plan, groups/guests/claims,
  lifecycle states, export/clone/port, checklists and troubleshooting. Roles table in the Operator
  Handbook and the events section of `docs/API.md` corrected to match the code.

### Added — dial-to-claim DECT handsets (GURU3-style)

- **Claim number + claim code.** `NumberPlan.dect_claim_number` (orga number plan form, API) turns the
  feature on. Every DECT extension gets a random 6-digit `Extension.dect_claim_code`
  (`DIAL_DECT_CLAIM_CODE_LENGTH`); the extension page shows *Claim a handset by dialing `9004123456`*
  plus a **New claim code** button (`portal:extension_new_claim_code`). Dialing it from any subscribed
  handset binds that handset to the extension, renumbers it on the OMM and announces the number - no
  IPEI typing. Dialing another extension's code moves the handset.
- **Claim pool.** `Device.unclaimed` marks handsets that are subscribed but belong to nobody. Orga adds
  them by IPEI on the DECT handsets page (*Add pool handset*, temp number `<claim>NNN`, display hint
  *Dial 9004+code*); handsets the OMM auto-created are adopted on the next sync
  (`apps.dect.claim.adopt_handset`, DIAL pushes its SIP identity to the OMM user).
- **PBX plumbing.** New hook kind `dect-claim`, realtime rows `<claim>` and `_<claim>X.`
  (`SERVICE_WITH_SUFFIX`), static `[dial-services] dect-claim` exten (DTMF fallback via `Read`).
  `DECTAdapter.update_subscription` accepts `sip_user`/`sip_password`; new optional `attach_user`.
  Webhooks `device.claimed`, `device.adopted`. Dummy backend: `add_foreign_handset()` for tests.
- Migrations: `devices 0003`, `extensions 0003`, `numbering 0003`.

### Changed — portal redesign (navigation & visual consistency)

- **Two-level navigation.** The top bar is now global only (DIAL · My extensions · Events · current
  event · Orga · account menu). Every event-scoped page (`/e/<slug>/…`) gets a left **sidebar**
  (`templates/_sidebar.html`) grouped into *Event*, *My stuff* and — for orga members — *Orga ·
  Numbers / People / Infrastructure / Services / Event*. Active entries are derived from the URL
  namespace via the new `{% nav_active %}` template tag (`apps/core/templatetags/dial_nav.py`), so
  views no longer need to pass an `active` variable. The 25-tab orga strip
  (`templates/portal/_orga_nav.html`) is gone. On screens < 1000 px the sidebar collapses behind a
  ☰ button.
- **Account menu.** Profile, API tokens, language and log-out live in one dropdown; the theme
  toggle stays as an icon button; the event switcher only appears with more than one event.
- **Consistent page headers.** `.page-header` → `.page-title` with optional `.eyebrow`
  (section, orange for orga), `<h1>` with `.count` pills, `.subtitle`; the event name was removed
  from titles because the sidebar carries the context.
- **Spacing system.** `static/css/dial.css` was rewritten around spacing tokens: top-level blocks
  are spaced automatically (`.page > * + *`), cards never touch, tables run edge-to-edge inside
  cards, headings inside cards get proper top spacing, `.form-actions` footers, `.field-submit`
  instead of `<label>&nbsp;</label>` hacks, `.empty` states, unified `.stat-card` tiles, tinted
  badges, alerts with an accent bar, `.card-danger`/`.btn-lg` for the emergency broadcast.
- All ~70 templates were normalised to these patterns; no URL names, view contexts or form fields
  changed. German strings for the new navigation labels were added to `locale/de`.

### Added (GURU3-parity batch)

GURU3-parity batch: eleven features that close the gap to Eventphone's GURU3 for everyday attendee
and orga workflows. Run `manage.py migrate`, then the new seed commands (see *Operations* below).

#### Features

- **Custom ringback tone** per extension: upload WAV/MP3/OGG/FLAC (max. 5 MB), converted in the
  background to 8 kHz mono (`ffmpeg` on the worker for non-WAV input); status Processing / Ready /
  Failed on the extension page, *Clear* to revert. Asterisk plays it as music-on-hold class
  `dial-<slug>-<number>` via Dial option `m(...)`; classes are rendered by
  `AsteriskPBX.render_musiconhold(event)` into the static `dial-moh.conf`.
  (`Extension.ringback_tone*`, docs: User Guide §6, Operator Handbook §9, Asterisk README)
- **Call forwarding** to another extension of the same event with modes *Always*, *Delayed*
  (ring own devices N seconds first), *When busy*, *When unanswered*. Self-targets, non-active
  targets and loops are rejected; forwarding is switched off automatically (audit-logged) when the
  target is deleted/expires/rejected; targets list *Forwarded from*. Legacy free-text forward fields
  remain and apply only when the mode is Off. (`Extension.forward_mode/forward_target/forward_delay`)
- **Per-extension toggles**: call waiting (off → `device_state_busy_at=1`), caller-ID display mode
  (number+name / name only / number only), DECT encryption (passed to the OMM as `encrypt`,
  unvalidated on hardware), announcement language en/de/event default
  (`Set(CHANNEL(language)=…)` in the dialplan preamble and `ps_endpoints.language`).
- **Prefix-free numbering** (`NumberPlan.prefix_free`, default on, toggle under *Number plan* /
  `/e/<slug>/numbering/settings/`): no extension may be a prefix of another (`23` vs `2323`), so
  every number dials without inter-digit timeout. Digit-only service numbers, emergency numbers and
  feature codes count as taken prefixes. The availability checker names the conflicting number
  (`conflicts` in `/api/v1/availability/`); orga may register off-length numbers but never prefix
  conflicts.
- **Random free number & extension pools**: "🎲 Give me a random free number" on the registration
  form; orga defines pools (prefix + total length) at `/e/<slug>/numbering/pools/`, otherwise the
  plan's default length range is sampled. API `GET /api/v1/random-number/?event=&type=` and portal
  `GET /e/<slug>/numbering/random/?type=`. (`ExtensionPool`)
- **Extension claims**: orga reserves a number for a person (account or e-mail) at
  `/e/<slug>/numbering/claims/`; invite link `/e/<slug>/numbering/claim/<token>/`. Open claims block
  the number (incl. prefix conflicts) for everyone else; redeeming activates the extension immediately
  with a policy override for restricted/approval ranges (never emergency/service/blocked/taken).
  Default validity 14 days, re-send/delete, redeemed claims link to the extension. (`ExtensionClaim`)
- **Call groups**: group admins (manage without owning the number), invitations by extension number
  with e-mail and accept/decline under *My call groups → invitations*
  (`/e/<slug>/callgroups/mine/`, `/invites/`, `/invites/<token>/`), members can leave, per-member
  ring delay for escalation tiers (ring-all; `Local/<delay>*<number>@dial-group` legs, route API
  `waves` / `dial_string_waves` / `callerid_prefix`), shortcode prefix on the caller name
  (`[SEC] Alice`), nested groups (max 3 levels, cycles rejected). API:
  `POST callgroups/{id}/invite/`, `GET callgroups/{id}/invites/`,
  `POST callgroups/{id}/invites/{ipk}/cancel/`, `GET callgroups/invites/?event=`,
  `POST callgroups/invites/{ipk}/respond/`, `GET|POST|DELETE callgroups/{id}/admins/`. Webhooks
  `callgroup.invited`, `callgroup.invite_accepted`, `callgroup.invite_declined`.
  (`CallGroup.admins/shortcode`, `GroupMember.delay_s`, `CallGroupInvite`)
- **DECT handset registry & history**: manufacturer recognised from the IPEI's EMC (first 5 digits)
  via the shared, crowdsourced `DECTManufacturer` table (`manage.py dial_dect_vendors` loads a
  best-effort builtin list); users suggest unknown vendors on the device page, orga curates at
  `/e/<slug>/devices/manufacturers/`. *My handsets* (`/e/<slug>/devices/history/`) lists handsets
  across events by IPEI with *Reuse in this event* (copies IPEI, model, label, UAK). New `Device.uak`.
- **GSM handsets**: per-event *has GSM* + PJSIP trunk name (`Event.gsm_trunk`, default
  `gsm-gateway`); GSM devices at `/e/<slug>/devices/gsm/` with IMSI/MSISDN and 2G–5G opt-ins and a
  6-digit one-time registration code; the GSM core links the SIM via `POST /prov/gsm/register/`
  (header `X-DIAL-PBX-Secret`, body `{event, token, imsi, msisdn?}`); bound extensions ring as
  `PJSIP/<msisdn>@<gsm_trunk>`. Commented `[gsm-gateway]` trunk template in `pjsip.conf`.
- **Autoprovisioning served by DIAL**: attach a `ProvisioningProfile` (builtins for Snom, Yealink,
  Grandstream, Cisco SPA via `manage.py dial_provisioning_profiles`); the device page shows
  `https://<dial>/prov/<token>/<filename>` (treat as a password). MAC-based
  `GET /prov/<vendor>/<mac>.cfg|.xml` requires `?token=` or HTTP Basic `sip_username:sip_password`
  and never serves credentials on the MAC alone. Filename patterns: Snom `{mac}.xml`, Yealink
  `{mac}.cfg`, Grandstream `cfg{mac}.xml`, Cisco `spa{mac}.cfg`. (`Device.provisioning_token`)
- **E-mail-confirmed signup**: `DIAL_REQUIRE_EMAIL_VERIFICATION=1` enables e-mail-first signup
  (address → link → nickname/password; existing addresses get a "you already have an account" mail,
  no enumeration). Default `0`: one-step signup + verification mail, unverified badge and resend
  button on the profile. E-mail changes always confirm via the new address (old address notified).
  Token TTL `DIAL_EMAIL_TOKEN_TTL_HOURS` (48); rate limits 5 mails/address/h, 20/IP/h.
  `manage.py dial_purge_tokens` or the daily beat task `accounts-purge-email-tokens` prunes tokens.
  (`RegistrationEmailToken`, `User.email_verified`)
- **PBX outbox (durable job queue)**: all PBX pushes (extension/device/event sync and removal, MWI,
  originate/broadcast) are `PBXJob` rows delivered by the worker on commit and by beat every 5 s;
  exponential backoff (10 s … 10 min cap), 5 attempts, then `dead` with `PBX: <error>` on the
  extension. Same-target edits coalesce. Orga dashboard widget, `GET /api/v1/pbx/outbox/?event=`,
  `POST /api/v1/pbx/outbox/retry/`, `dial pbx outbox --event <slug> [--retry-dead]`, admin actions
  *Retry* / *Deliver now*. Delivered jobs purged after 7 days. Eager/dev mode delivers synchronously;
  override with the Django setting `DIAL_PBX_OUTBOX_SYNC`.

### Changed

- Generated endpoint extens carry one extra preamble row `Set(CHANNEL(language)=…)` at priority 6;
  the `Dial` moved from priority 6 to 7 and the busy/no-answer branches shifted by one. Adjust any
  custom `Goto`s into `dial-<slug>` extens.
- `musiconhold.conf` now `#include`s `dial-moh.conf`; operators write `render_musiconhold(event)`
  output there and run `moh reload`. Asterisk needs read access to `MEDIA_ROOT/ringback/processed/`.
- `[dial-group]` gained the delayed-leg exten `_XXX*X.` and honours `dial_string_waves` /
  `callerid_prefix` from the route API (older responses without them keep working).
- The `gsm` endpoint type is enabled per event (`Event.has_gsm`) instead of being globally disabled.
- `/api/v1/availability/` responses include `conflicts` and `reserved`.
- `Webhook.EVENT_TYPES` gained the three `callgroup.*` types.

### Operations

- New settings: `DIAL_REQUIRE_EMAIL_VERIFICATION`, `DIAL_EMAIL_TOKEN_TTL_HOURS` (environment) and
  `DIAL_PBX_OUTBOX_SYNC` (Django setting, not read from the environment).
- New management commands: `dial_provisioning_profiles`, `dial_dect_vendors`, `dial_purge_tokens`.
- New beat entries: `pbx-outbox-drain` (5 s), `pbx-outbox-purge-delivered` (daily),
  `accounts-purge-email-tokens` (daily).
- New migrations in `accounts`, `callgroups`, `devices`, `events`, `extensions`, `numbering`, `pbx`.
