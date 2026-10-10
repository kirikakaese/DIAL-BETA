# DIAL REST API

Base URL: `/api/v1/`. Interactive docs: **Swagger UI at `/api/docs/`**, ReDoc at `/api/redoc/`, raw schema
at `/api/schema/`. A generated copy lives in [`api/openapi.yaml`](api/openapi.yaml)
(`make openapi`). Developer contracts for the PBX side are in [`DEVELOPING.md`](DEVELOPING.md).

## Authentication

| Method | Use |
|---|---|
| Session cookie (+ CSRF token) | the browser portal and quick experiments in Swagger UI after logging in |
| `Authorization: Bearer dial_...` | service accounts for integrations (badge printers, info-beamers, CLI) |

Service tokens are minted once and stored hashed (SHA-256). Mint them at `/accounts/profile/tokens/`
(personal, all your rights), at `/e/<slug>/orga/tokens/` (event-bound), or on the server:

```sh
manage.py dial_token --user admin@dial.local --name badge-printer --event demo \
    --scopes extensions:read phonebook:read --expires-days 14 --export
# → export DIAL_TOKEN=dial_...
```

**Scopes** are `<app>:read` / `<app>:write` (e.g. `extensions:write`, `dect:read`, `pages:read`,
`pages:write`, `phonebook:write`), `<app>:*` or `*`; the venue agent uses the special scope `pbx:sync`
(snapshot, schema and heartbeat endpoints only).
Single sign-on (OpenID Connect) is a browser-only login; there is no token exchange for the API - service
tokens are minted and used exactly as before.
An empty scope list means "everything the owner may do". Views declare
`required_scopes = {"get": ["<app>:read"], "default": ["<app>:write"]}`; tokens are further restricted by
the owner's role in the event and, if set, the token's event.

## Conventions

- **Pagination**: `LimitOffsetPagination` - `?limit=50&offset=100` (default page size 50); responses are
  `{count, next, previous, results}`.
- **Event scoping**: list endpoints take `?event=<slug>`; many objects embed `event` (slug) in their
  payload.
- **Filtering / search / ordering**: django-filter fields per viewset (e.g. `?state=requested&type=dect`),
  `?search=` on name/number fields, `?ordering=-created_at`.
- **Throttling**: anonymous 60/min, authenticated 600/min (DRF); portal-level per-IP limits on login,
  registration and availability. DRF throttle responses (`429`) carry a `Retry-After` header; the
  middleware returns a plain `429 Too many requests`.
- **Errors**: standard DRF JSON (`{"detail": ...}` or field → messages). Policy denials on extension
  creation return `400` with the `PolicyResult.reason`.
- **IDs**: extensions, devices, events and users use UUIDs; events are addressed by `slug`.

## Endpoint overview

Who: **any** = anonymous, **user** = authenticated member, **owner** = object owner or orga,
**orga** = event orga/admin, **pbx** = Asterisk with `X-DIAL-PBX-Secret`.

### Core (`apps/api`)

| Method | Path | Purpose | Who |
|---|---|---|---|
| GET | `availability/?event=&number=&type=` (`&block_digits=1|2|3` with `type=trunk`) | live policy + availability check (incl. `conflicts`, `reserved`); for trunks the whole block is checked | any |
| GET | `random-number/?event=&type=` | a random, instantly registrable number from the event's extension pools (`{"number": "4711"}`, `null` if none) | any |
| GET | `health/?event=` | DIAL, PBX and DECT backend health. Without `event` the **server default** adapters are checked; with `?event=<slug>` the event's venue connection (`404` for an unknown slug). Response carries `event` | any |
| GET | `me/` | current user / service account; `token` describes the calling service token (`{name, prefix, scopes, event, expires_at}`, `null` in a browser session) | user |
| GET, POST | `events/` | list / create events | user / admin |
| GET, PATCH, DELETE | `events/{slug}/` | event details / edit / delete. Scheduled lifecycle: `registration_opens_at`, `goes_live_at`, `archives_at` (ISO 8601 or `null`; validated against the current state) and read-only `next_scheduled_transition` (`{"state", "at"}` or `null`); a beat task applies due schedules every minute | user / orga / admin |
| GET | `events/{slug}/number-plan/` | plan + ranges, service numbers (`dect_claim_number`, `announcement_record_number`, ...) and phone feature codes (`callback_*_code`, `group_login/logout_code`, `forward_set_code` `*21`, `forward_clear_code` `*20`, `forward_busy_code` `*22`, `forward_noanswer_code` `*23`) | user |
| GET | `events/{slug}/members/`, `events/{slug}/groups/` | memberships with the member's `email` (helpdesk+), user groups | helpdesk / user |
| POST | `events/{slug}/transition/` `{"state": "live"}` | lifecycle change | orga |
| POST | `events/{slug}/clone/` | clone into new draft event | admin |
| GET | `events/{slug}/audit/` | audit log | orga |
| GET | `events/{slug}/export/` | full JSON export (incl. info `pages`) | orga |
| GET, POST | `extensions/?event=` | list / request extension (`number`, `type`, `display_name`, ...). `type=trunk` registers a SIP trunk number block: write `block_digits` (1–3 trailing wildcard digits = 10/100/1000 numbers, fixed after creation), read `block_range` (`["4700", "4799"]`, `null` for other types) | user |
| GET, PATCH, DELETE | `extensions/{id}/` | details / edit / delete (incl. `forward_mode`, `forward_target`, `forward_delay`, `call_waiting`, `callerid_display`, `dect_encryption`, `language`) | owner |
| POST | `extensions/{id}/approve|reject|suspend|reactivate/` | moderation | orga |
| POST | `extensions/{id}/provision/` | re-push to PBX/DECT | orga |
| POST | `extensions/{id}/transfer/` `{"to_user": ...}` | transfer ownership | owner |
| POST | `extensions/{id}/bind-device/` `{"device": id, "priority": 0}` | multi-device binding | owner |
| GET | `extensions/portable/?event=` | numbers from earlier events you can port | user |
| POST | `extensions/{id}/port/` `{"event": slug}` | re-request in another event | owner |
| POST | `extensions/import/?event=` `{"csv": "<text>", "dry_run": false, "create_users": false, "allow_ownerless": true}` | bulk CSV import of extensions (and missing users). Header columns `number,type,display_name,email,username,location,in_phonebook,group,role` (aliases accepted). Response `{plan: [{line, number, type, display_name, owner, owner_exists, create_user, new_username, ownerless, group, role, action, messages, extension_id}], applied, users_created, skipped, errors, dry_run, unknown_columns}`; `action` is `create|created|skip-taken|error`. With `dry_run` nothing is written and `errors` lists the rows that would not be created | orga |
| GET | `extensions/history/?event=&number=` | timeline of one number: `{number, event, extensions: [...], audit: [...], timeline: [{kind: "extension"\|"audit", at, ...}], other_events}` - every extension that carried the number in this event and in events the caller staffs, plus their audit entries | helpdesk |
| GET, POST | `devices/?event=` | list / create device (`type`, `ipei` or SIP) | user |
| GET, PATCH, DELETE | `devices/{id}/` | device details incl. SIP credentials for owner; for SIP devices `softphone_links` (`{generic, linphone, acrobits}` QR payloads - the `linphone`/`acrobits` entries point at `/prov/<token>/…xml` and are empty until the device has a provisioning token) | owner |
| POST | `devices/{id}/new-pin/` | new DECT subscription PIN | owner |
| POST | `devices/{id}/rotate/` | rotate SIP password (+ resync) | owner |
| GET | `devices/{id}/qr/?client=generic\|linphone\|acrobits` | softphone QR (PNG) of the chosen `softphone_links` entry (default `generic`; `404` for an unknown client) | owner |

### PBX (`apps/pbx`)

| Method | Path | Purpose | Who |
|---|---|---|---|
| POST | `pbx/hooks/{kind}/` (`feature-code`, `extension-idle`, `cdr`, `voicemail`, `site-survey`, `dect-claim`, `announcement-record-start`, `announcement-recorded`) | Asterisk → DIAL events | pbx |
| GET | `pbx/route/?event=&number=` | dial string / strategy / IVR for dynamic numbers; numbers inside a SIP trunk block (base or wildcard part) come back as `type: "trunk"` with `trunk: {base, range}` and the dial string towards the remote PBX | pbx |
| GET | `pbx/dialplan/?event=` (`&shell=1`) | rendered dialplan / shell contexts | pbx, orga |
| POST | `pbx/resync/?event=` | rewrite all realtime rows | orga |
| GET | `pbx/status/?event=` | backend health, channels, registrations | orga |
| GET | `pbx/outbox/?event=` | PBX outbox: counts per state (`pending/sending/failed/dead/delivered`, `backend`, `sync_mode`) + 20 most recent `PBXJob`s | orga |
| POST | `pbx/outbox/retry/?event=` | re-queue all dead jobs of the event (`{"event", "retried", "stats"}`) | orga |
| GET, PUT/PATCH, DELETE | `pbx/connection/?event=` | the event's **venue connection** (what `/e/<slug>/pbx/` edits). `GET` → `{pbx, dect, effective, server_default, backends}`; secrets come back only as `has_ari_password` / `has_hook_secret` / `has_password` flags. `PUT`/`PATCH` body `{"pbx": {backend, provisioning, agent_poll_interval, ari_url, ari_user, ari_password, ari_app, ami_host, ami_port, ami_user, ami_password, hook_secret, notes}, "dect": {backend, host, port, user, password, verify_tls, notes}}` - either part optional, partial updates merge, omitted/empty secrets keep the stored value, same validation as the orga page (`400` with `errors`). `provisioning` is `shared_db` (default: the venue Asterisk reads DIAL's PostgreSQL) or `agent` (a venue agent pulls snapshots, see below); `agent_poll_interval` in seconds (min 5). The `pbx` part also carries the read-only agent status `agent_last_seen`, `agent_version` (applied snapshot version), `agent_host`, `agent_software`, `agent_asterisk_ok`, `agent_message` plus the computed `agent_is_stale` (no heartbeat within 3 poll intervals), `agent_behind` (applied ≠ current) and `snapshot_version`. `DELETE ?part=pbx\|dect\|all` removes the connection(s) → server default. Scopes `pbx:read` / `pbx:write`; audited. | orga |
| GET | `pbx/snapshot/?event=` | **venue agent** snapshot of all realtime rows of the event: `{event, version, generated_at, poll_interval, tables: {ps_endpoints, ps_auths, ps_aors, ps_endpoint_id_ips, extensions, voicemail_users}}`, each table `{key, rows}` (`key` = primary-key column). `version` is a content hash that ignores the volatile `voicemail_users.stamp` and `extensions.id`; it is also sent as `ETag`, and `If-None-Match` (strong or `W/`) with the current version yields `304` without a body | agent (`X-DIAL-PBX-Secret` = the event's hook secret), service token with scope `pbx:sync`, or orga session; wrong credentials `401`, unknown event `404` |
| GET | `pbx/snapshot/schema/?event=` | `{dialect: "postgresql", sql, tables}` - `CREATE TABLE IF NOT EXISTS` DDL for the agent's local database (`ps_endpoints`, `ps_auths`, `ps_aors`, `ps_contacts`, `ps_endpoint_id_ips`, `extensions`, `voicemail_users`, `cdr`); same output as `manage.py pbx_venue_schema` | agent / `pbx:sync` / orga |
| POST | `pbx/agent/heartbeat/` `{event, version, hostname, agent_version, asterisk_ok, message, contacts?}` | agent check-in (JSON or form): stores the `agent_*` fields on the event's `PBXConnection`; optional `contacts` (rows of the venue's `ps_contacts`) replace DIAL's copy for the event's endpoints so registration status keeps working. Response `{ok, event, connection, current_version, poll_interval, behind}` (`connection` = the event has a PBX connection row, `behind` = applied `version` ≠ `current_version`). Not audited | agent / `pbx:sync` / orga |

### DECT (`apps/dect`)

| Method | Path | Purpose | Who |
|---|---|---|---|
| GET | `dect/rfps/`, `dect/rfps/{id}/` | RFPs (read-only; positions are edited on the venue map). List filters: `?event__slug=`, `connected`, `synced`, `is_active`, `cluster__cluster_id`, `?search=` | user |
| GET | `dect/clusters/` | sync clusters with health | user |
| GET, PATCH | `dect/alerts/` | alerts, resolve | orga |
| GET | `dect/handsets/` | handsets with owner, extension, last RFP, battery/RSSI | orga |
| POST | `dect/sync/?event=` | poll OMM now | orga |
| GET | `dect/coverage/?event=` | coverage summary + weak zones | user |

### Callback (`apps/callback`)

| Method | Path | Purpose | Who |
|---|---|---|---|
| GET, POST | `callback/requests/` | CCBS/CCNR requests; `POST {id}/cancel/` | owner |
| GET, POST | `callback/scheduled-calls/` | wake-up calls; `POST {id}/snooze/`, `{id}/cancel/` | owner |
| POST | `callback/test-ringback/` `{"event":..., "number":...}` | schedule a test ringback | user |
| POST | `callback/result/` | originate result from PBX | pbx |

### Phonebook, call groups, voicemail, stats

| Method | Path | Purpose | Who |
|---|---|---|---|
| GET | `phonebook/?event=&q=&type=` | public entries (`{event, count, results}`); each entry carries `number`, `number_label` (`4700–4799` for trunk blocks, else the number), `name`, `type`, `type_display`, `description`, optional `location`/`owner`/`category`, and the business card links `vcard_url` / `card_qr_url` (absolute, rooted at `DIAL_PUBLIC_URL`) | any |
| GET | `phonebook/export.{csv|vcf|ldif|pdf}?event=` | exports | any |
| GET | `phonebook/directory/?event=` | **remote directory** for desk phones / the DECT OMM: `{event, enabled, token, urls: {snom, yealink, grandstream, cisco, mitel, generic}, ldap: {host, port, base_dn, bind_dn, password, name_attributes, number_attribute}}` - the per-vendor XML URLs (`/e/<slug>/phonebook/remote/<token>/<vendor>.xml`, absolute) and what to type into a phone's LDAP settings (`password` = the directory token). `enabled` mirrors `PhonebookSettings.directory_enabled` (orga phonebook settings page). Scopes `phonebook:read` | orga |
| POST | `phonebook/directory/rotate/?event=` | mint a new directory token (every phone, OMM and LDAP client configured with the old one is locked out); returns the same payload as `GET directory/`. Scope `phonebook:write`; audited | orga |
| GET, POST | `callgroups/?event=` | groups (`shortcode`, `admins`, ...); `GET {id}/members/`, `GET {id}/targets/` (`targets`, `waves`, `callerid_prefix`) | user / orga |
| POST | `callgroups/{id}/add-member|remove-member|login|logout/` | membership & presence (members may carry a ring `delay_s`; a member may itself be a group, max 3 levels) | owner / orga |
| GET, POST, DELETE | `callgroups/{id}/admins/` `{"user": "<e-mail or nickname>"}` | list / appoint / remove group admins | owner / orga (GET: manager) |
| POST | `callgroups/{id}/invite/` `{"number": "4242", "reason": "..."}` | invite an extension by number (owner gets an e-mail) | manager |
| GET | `callgroups/{id}/invites/` | invitations of this group | manager |
| POST | `callgroups/{id}/invites/{ipk}/cancel/` | withdraw an open invite | manager |
| GET | `callgroups/invites/?event=` | open invitations for my extensions | user |
| POST | `callgroups/invites/{ipk}/respond/` `{"accept": true}` | accept / decline an invite for my extension | owner of the invited extension |
| GET, POST, PATCH | `voicemail/mailboxes/` | mailbox settings | owner |
| GET, DELETE | `voicemail/messages/`; `POST {id}/mark-read|mark-unread/`; `GET {id}/audio/` | messages | owner |
| GET | `stats/summary|hourly|extensions|rfps/?event=` | dashboards (aggregate) | orga (summary: user) |
| GET | `stats/me/export/?event=`, DELETE `stats/me/` | GDPR export / delete of own call data | user |

### Info pages (`apps/pages`)

Orga-written Markdown pages shown under `/e/<slug>/pages/`; scopes `pages:read` / `pages:write`.

| Method | Path | Purpose | Who |
|---|---|---|---|
| GET, POST | `pages/?event=` | list (`event` required; unpublished pages only for orga) / create `{event, slug, title, body, order?, published?, show_on_dashboard?}` | user / orga |
| GET, PATCH, DELETE | `pages/{id}/` | page details / edit / delete. Fields: `id`, `event`, `slug`, `title`, `body` (Markdown), read-only `body_html` (rendered, escaped), `order`, `published`, `show_on_dashboard`, `updated_at`. Slugs `manage`/`new` are reserved, `(event, slug)` is unique, a page cannot move to another event | user / orga |

### Flag-gated apps

| Method | Path | Flag | Who |
|---|---|---|---|
| GET, POST | `messaging/messages/`; `POST messaging/broadcast/` | `messaging` | user / orga |
| CRUD | `ivr/announcements/`, `ivr/menus/` (orga see the event's with `?event=<slug>`); `GET ivr/announcements/{id}/audio/` - the audio file (upload or recording by phone; `404` without audio), scope `ivr:read`, owner or orga (`?event=`) | `ivr` | owner |
| CRUD | `conferences/rooms/`; `GET {id}/participants/`; `POST {id}/kick/` | `conferences` | owner |
| CRUD | `federation/peers/`; `GET {id}/pjsip/`; `GET federation/directory/` | `federation` | orga / any |
| CRUD | `breakout/trunks/` (`GET {id}/pjsip/`), `breakout/rules/`, `breakout/permissions/`; `GET breakout/usage/` | `breakout` | orga |
| CRUD | `emergency/targets/`, `emergency/incidents/` (`POST {id}/resolve/`); `POST emergency/broadcast/`; `POST emergency/incident-log/` (PBX hook, `X-DIAL-PBX-Secret`) | `emergency` | orga |

## Example: availability check

```sh
curl "http://localhost:8000/api/v1/availability/?event=demo&number=2323&type=dect" \
     -H "Authorization: Bearer $DIAL_TOKEN"
```

```json
{"number": "2323", "available": false, "taken": false, "requires_approval": false,
 "reason": "Range 'Angels' is restricted to specific roles or groups.", "range": "Angels",
 "suggestions": [], "conflicts": [], "reserved": false}
```

`available` also requires the event to be in `registration`/`live` and the user's quota to be fine;
`suggestions` lists nearby free numbers when the requested one is taken. With prefix-free numbering
(`NumberPlan.prefix_free`) `conflicts` names the existing numbers that are a prefix of / extend the
requested one (e.g. `["23"]` when asking for `2323`) and `taken` is `true`; `reserved` is `true` when an
open extension claim blocks the number for other users. Anonymous calls are allowed
(role-based checks then assume no membership). Create the extension with
`POST /api/v1/extensions/ {"event": "demo", "number": "4323", "type": "dect", "display_name": "Alice"}`,
or let DIAL pick: `GET /api/v1/random-number/?event=demo&type=dect` → `{"number": "4711"}`.

## Webhooks

Configure subscriptions at `/e/<slug>/orga/webhooks/` (URL, secret, event types; empty = all). Types:
`extension.created|approved|rejected|updated|deleted|transferred`,
`callgroup.invited|invite_accepted|invite_declined`, `device.provisioned|claimed|adopted`,
`dect.rfp.down|rfp.up|sync.degraded`, `callback.completed`, `emergency.triggered`, `page.updated` (info page
created or edited; `data` = the page fields plus `action: "create"|"update"`), `announcement.recorded`
(recorded by phone; `data` = `{extension, event, audio, file, duration, imported, announcement}`; fetch the file
with `GET ivr/announcements/<announcement>/audio/?event=<slug>`), `pbx.snapshot.changed`
(the venue-agent snapshot version changed after a PBX write; `data` = `{event, version, previous}`; only for
events whose PBX connection uses `provisioning: agent`). Delivery is a Celery task with 5 retries (30 s
backoff); the last status is shown in the UI.

Request: `POST <url>`, `Content-Type: application/json`, headers `X-DIAL-Event: <type>`,
`X-DIAL-Delivery: <uuid>` (the same for every retry of one delivery: drop repeats by it; `sent_at` in the body
changes per attempt) and, when a secret is set, `X-DIAL-Signature: sha256=<hex HMAC-SHA256 of the raw body>`.

```json
{"type": "extension.approved", "sent_at": "2026-09-14T12:00:00+00:00",
 "data": {"id": "…", "event": "demo", "number": "4242", "type": "sip", "state": "active",
          "owner": "alice", "display_name": "Alice (desk)"}}
```

Verification (Python):

```python
import hmac, hashlib
def verify(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")
```

## PBX hook & route contracts

Asterisk calls `POST /api/v1/pbx/hooks/<kind>/` (form-encoded, header `X-DIAL-PBX-Secret`) and
`GET /api/v1/pbx/route/?event=&number=`; responses are JSON. For call groups the route response also
carries `waves` (members grouped by ring delay), `dial_string_waves` (ring-all string with
`Local/<delay>*<number>@dial-group` legs for delayed members) and `callerid_prefix` (`"[SEC] "` from the
group shortcode).

Recording announcements by phone (`NumberPlan.announcement_record_number` + the per-announcement
`announcement_record_code`, dialed as `<record number><code>`):

| Hook | Request fields | Response |
|---|---|---|
| `POST pbx/hooks/announcement-record-start/` | `event`, `caller` (PJSIP endpoint name), `callerid` (`CALLERID(num)`), `code` | `{"handled": true, "number": "<announcement>", "name": "<slug>-<number>-<timestamp>", "file": "<DIAL_RECORDING_DIR>/<name>"}` (path without extension - Asterisk `Record`s there); `{"handled": false}` for an unknown code, a caller that is not an endpoint of the event, or a caller who is neither the announcement's owner nor helpdesk |
| `POST pbx/hooks/announcement-recorded/` | `event`, `code`, `file` (or `file_path`), `duration` (s) | `{"handled": true, "number": "<announcement>"}`; DIAL copies the wav into media storage when it can read it (shared `recordings` volume), otherwise keeps the PBX path as playback reference, re-provisions the announcement and emits `announcement.recorded` |

The exact bodies, response keys and the
dialplan contexts that use them are documented in
[`deploy/asterisk/README.md`](../deploy/asterisk/README.md#hooks-and-route-api-asterisk--dial) and the
service functions they dispatch into in [`DEVELOPING.md`](DEVELOPING.md#pbx--dial-contracts).

## Provisioning & GSM endpoints (`/prov/`, outside `/api/v1/`)

These live under `/prov/` because phones and cell cores cannot log in; the secret is in the URL or a
header instead (`apps/devices/prov_urls.py`, `prov_views.py`).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/prov/<token>/<filename>` | the per-device `provisioning_token` in the path; `<filename>` must match the profile's pattern | rendered autoprovisioning file for the device (shown on the device page - treat the URL as a password) |
| GET | `/prov/<token>/linphone.xml` | the SIP device's `provisioning_token` in the path | Linphone remote-provisioning XML (`lpconfig`); the QR payload is `linphone-config:<this URL>` (`softphone_links.linphone`) |
| GET | `/prov/<token>/acrobits.xml` | the SIP device's `provisioning_token` in the path | Acrobits / Groundwire / Cloud Softphone `<account>` XML; the QR payload is the URL itself (`softphone_links.acrobits`) |
| GET | `/prov/<token>/phonebook.xml` (`?vendor=snom\|yealink\|grandstream\|cisco\|mitel\|generic`, `?q=`) | the device's `provisioning_token` in the path | remote phonebook of the device's event in the XML dialect of its provisioning profile's vendor (`?vendor=` overrides, else `generic`); `?q=`/`?search=`/`?name=` filter by name fragment. `404` when the device or token is unknown, the phonebook feature is off or the event's remote directory is disabled. The builtin templates reference this URL (`phonebook_url`), so the event-wide directory token never appears in a config file |
| GET | `/prov/<vendor>/<mac>.cfg` or `.xml` (`vendor` = `snom|yealink|grandstream|cisco|generic`) | `?token=<provisioning token>` **or** HTTP Basic `sip_username:sip_password`; otherwise `401` with a help text | MAC-based lookup for phones that only know their MAC. Credentials are never served on the MAC alone |
| POST | `/prov/gsm/register/` | header `X-DIAL-PBX-Secret` (same as the Asterisk hooks) | body `{"event": "<slug>", "token": "<6-digit code>", "imsi": "...", "msisdn": "..."}` (JSON or form-encoded) links the SIM to the GSM device holding `token`; `{"registered": true, ...}` or `404 {"registered": false, "detail": ...}` |

Filename patterns of the builtin profiles: Snom `{mac}.xml`, Yealink `{mac}.cfg`, Grandstream
`cfg{mac}.xml`, Cisco SPA `spa{mac}.cfg`. Responses are `Cache-Control: no-store`.

## Portal-only endpoints worth knowing

Not part of the REST API but useful for integrators and kiosks (session auth):

- `GET /manifest.webmanifest`, `GET /sw.js`, `GET /offline/` - the PWA shell (no auth; `/sw.js` answers 404
  when `DIAL_PWA_ENABLED=0`). The service worker caches only an allowlist of read-only pages and never
  anything under `/api/`, `/accounts/`, `/prov/` or orga/PBX pages.

- `GET /e/<slug>/numbering/random/?type=` - random free number for the registration form.
- `/e/<slug>/numbering/claim/<token>/` - redeem an extension claim (invite link sent by orga).
- `/e/<slug>/callgroups/invites/<token>/` - accept/decline a call group invite from the e-mail link.
- `GET /e/<slug>/phonebook/<number>.vcf` - vCard of one phonebook entry (business card; `vcard_url` in the
  phonebook API).
- `GET /e/<slug>/phonebook/<number>/qr.png` - QR code for that entry (`card_qr_url`).
- `/e/<slug>/phonebook/<number>/card/` - printable business card of the extension.
- `/e/<slug>/pages/`, `/e/<slug>/pages/<page-slug>/` - info pages; orga edits them under `/e/<slug>/pages/manage/`.
- `GET /e/<slug>/phonebook/remote/<token>/<vendor>.xml` - **remote phonebook** for desk phones and the DECT
  OMM, no login: the event's directory token (16–64 URL-safe chars, `PhonebookSettings.directory_token`) is
  the credential. `vendor` = `snom` (`SnomIPPhoneDirectory`), `yealink` (`YealinkIPPhoneDirectory`),
  `grandstream` (`AddressBook/Contact`), `cisco` (`CiscoIPPhoneDirectory`), `mitel` (`IPPhoneDirectory` for
  the SIP-DECT OMM corporate directory) or `generic` (`IPPhoneDirectory`); `?q=` / `?search=` / `?name=`
  filter by name fragment. Served only while the event is in *registration* or *live* (same rule as LDAP). Every failure (unknown or closed event, feature off, directory disabled, wrong token,
  unknown vendor) is a plain `404`. The URLs are listed on the orga phonebook settings page
  (`/e/<slug>/phonebook/settings/`) and in `GET /api/v1/phonebook/directory/`.
- `POST /e/<slug>/phonebook/settings/rotate-token/` - orga: rotate the directory token (same as
  `POST /api/v1/phonebook/directory/rotate/`).
- `/accounts/oidc/login/` (`?next=`; `link=1` for a logged-in user links the identity to the current
  account), `/accounts/oidc/callback/`, `POST /accounts/oidc/unlink/` - **OpenID Connect login**
  (authorization code + PKCE, `DIAL_OIDC_*` settings; `manage.py dial_oidc_check` prints the provider endpoints
  and the redirect URI to register). Browser sessions only: there is **no** API token exchange for SSO,
  service tokens are unchanged. With `DIAL_OIDC_ALLOW_PASSWORD_LOGIN=false` (SSO-only) the password form,
  the signup form and the header's *Sign up* button are hidden and `POST /accounts/login/` /
  `POST /accounts/register/` answer `403`.

### LDAP directory (not HTTP)

`manage.py dial_ldap [--host H] [--port P] [--cert crt.pem [--key key.pem]]` (compose service `ldap`,
port **3890**; `--cert` switches the same port to LDAPS) serves every *registration*/*live* event whose
remote directory is enabled as a read-only LDAP v3 tree. Settings `DIAL_LDAP_HOST`, `DIAL_LDAP_PORT`,
`DIAL_LDAP_ALLOW_ANONYMOUS` (default off), `DIAL_LDAP_CACHE_SECONDS` (default 30). What a phone or the OMM
needs (also returned as `ldap` by `GET /api/v1/phonebook/directory/`):

| Setting | Value |
|---|---|
| Bind DN | `cn=directory,dc=<slug>,dc=dial` (simple bind) |
| Bind password | the event's directory token |
| Search base | `ou=phonebook,dc=<slug>,dc=dial` |
| Entry DN | `cn=<name>+telephoneNumber=<number>,ou=phonebook,dc=<slug>,dc=dial` (`inetOrgPerson`) |
| Attributes | `cn`, `sn`, `givenName`, `displayName`, `telephoneNumber`, `mobile`, `uid` (= number), `description`, `l` (location hint), `ou` (phonebook category), `o` (event name), `title` (extension type) |
| Name / number attributes | `cn sn` / `telephoneNumber` |

The root DSE lists `namingContexts` only for events the connection may search; a bind to a disabled or
archived event fails with `invalidCredentials`. Only simple bind is supported (no SASL, no StartTLS - use
LDAPS), write operations answer `unwillingToPerform`.

## CLI

The `dial` console script (`apps/api/cli.py`, installed with the project) wraps the API:

```sh
export DIAL_URL=http://localhost:8000 DIAL_TOKEN=dial_...     # or --url / --token
dial health [--event demo]          # DIAL, PBX, DECT health (--event: that event's venue instead of the server default)
dial me
dial events list | show <slug> | transition <slug> <draft|registration|live|archived> | export <slug>
dial events schedule <slug> [--registration ISO] [--live ISO] [--archive ISO] [--clear]   # PATCH the lifecycle schedule; --clear removes all, --live "" clears one
dial extensions list --event demo [--state requested] [--type dect] [--search alice]
dial extensions create --event demo --number 4323 [--type sip] [--display-name ...] [--location ...]
dial extensions create --event demo --number 4700 --type trunk --block-digits 2   # SIP trunk block 4700–4799
dial extensions approve|reject <id> [--note ...]
dial extensions delete <id>
dial extensions import --event demo --file people.csv [--dry-run] [--create-users]   # POST /extensions/import/
dial devices list --event demo [--type sip] [--state subscribed]
dial queue --event demo             # extensions awaiting approval
dial phonebook --event demo [--search bar]
dial phonebook directory --event demo [--rotate]   # remote-phonebook URLs (desk phones / OMM) + LDAP details; --rotate mints a new token first
dial dect rfps|handsets|sync --event demo
dial dect alerts --event demo [--open]
dial resync --event demo            # push the whole event to the PBX
dial pbx outbox --event demo [--retry-dead]   # PBX outbox stats + recent jobs; re-queue dead jobs first
dial pbx connection show --event demo         # venue PBX/DECT connection (server default vs. configured)
dial pbx connection set --event demo --pbx '{"backend":"asterisk","ari_url":"http://10.1.1.5:8088/ari","ari_user":"dial","ari_password":"...","hook_secret":"..."}' [--dect '{"backend":"omm","host":"10.1.1.9","password":"..."}']
dial pbx connection reset --event demo [--part pbx|dect|all]
dial pbx connection set --event demo --provisioning agent|shared_db [--agent-poll-interval 15]   # switch to / from venue-agent snapshot sync
dial pbx agent status --event demo                # venue agent: last heartbeat, applied vs. current snapshot version, host, Asterisk state
dial pbx snapshot --event demo [--out snapshot.json]   # dump GET /pbx/snapshot/
dial pages list --event demo                  # info pages (slug, title, order, published)
dial pages show <slug> --event demo           # one page with its Markdown body
```

Server-side helpers: `manage.py dial_token` (mint tokens), `manage.py phonebook_ldif --event <slug>
[--format csv|vcf|ldif|pdf] [--out file]`, `manage.py seed_demo`, `manage.py dial_provisioning_profiles
[--update]` (builtin autoprovisioning templates; `--update` rewrites existing builtin profiles to the shipped
version, e.g. to pick up the phonebook keys), `manage.py dial_dect_vendors` (DECT manufacturer codes),
`manage.py dial_purge_tokens` (expired e-mail confirmation tokens), `manage.py dial_ldap` (LDAP phonebook
server, see above), `manage.py dial_oidc_check` (verify the OpenID Connect configuration),
`manage.py pbx_venue_schema [--out file.sql]` (PostgreSQL DDL for a venue agent's local database).

The venue side of the snapshot sync is `deploy/venue-agent/dial_venue_agent.py` (+ `Dockerfile`, systemd unit
`dial-venue-agent.service`, `README.md`): configured by environment (`DIAL_URL`, `DIAL_EVENT`,
`DIAL_PBX_HOOK_SECRET` **or** `DIAL_SYNC_TOKEN` = a service token with scope `pbx:sync`, `DATABASE_URL` or
`DB_*`, `POLL_INTERVAL`, `ASTERISK_RELOAD` / `AMI_*`), `--once` runs one cycle, `--check` prints connectivity
and schema status. `deploy/asterisk/docker-compose.venue.example.yml` + `.env.venue.example` run `venue-db`,
`venue-agent` and the Asterisk image together.

## OpenAPI

`docs/api/openapi.yaml` is generated with `manage.py spectacular --file docs/api/openapi.yaml`
(CI validates it). Function-based views without serializers appear with generic request/response
schemas; the tables above and Swagger UI describe their parameters.
