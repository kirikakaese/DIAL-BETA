# Operator Handbook - Running DIAL at your event

This is the guide for the orga / NOC team. It assumes you have read the [README](../README.md) and can
run `docker compose`. Attendee-facing instructions are in the [User Guide](USER_GUIDE.md).

## 1. Timeline

| When | What |
|---|---|
| T-8 weeks | Pick hardware (server, RFPs, OMM licence, handsets). Deploy DIAL on a staging box, create the event as **draft**, design the number plan, create groups. Import last year's export if you have one. |
| T-6 weeks | Open **registration**. Announce the URL. Set up moderation rota for the queue. |
| T-3 weeks | Rehearse: register a handset against a test OMM, run the Asterisk stack, dial 9000/9003. Pre-pull images for the venue. |
| T-1 week | Freeze number plan changes (or announce them). Export JSON + Postgres dump. |
| Build-up | Install RFPs, upload the venue map, place RFPs, walk the site with the survey number. Switch event to **live**. |
| Event | Watch `/e/<slug>/dect/` and alerts, staff the helpdesk, keep the queue short. |
| T+1 week | Export stats/CDR you want to keep, run retention, switch to **archived**, back up. |

## 2. Deployment options

**The shape of a DIAL deployment.** DIAL is *one* permanent service under a fixed domain (say
`dial.example.org`) that hosts many events. Every event brings its **own phone infrastructure at the
venue** - an Asterisk box plus, if there are handsets, a Mitel OMM with RFPs. Those venue boxes are
connected to DIAL **per event**, by the event's orga, on `/e/<slug>/pbx/` (*PBX & DECT connection* in the
sidebar): ARI/AMI URL and credentials for Asterisk, host/user/password for the OMM, and a per-event hook
secret the venue Asterisk presents when it calls DIAL. Events without such a connection use the server-wide
`ASTERISK`/`OMM` settings from `.env` - fine for a single-event box or a lab, and what the demo uses.

What has to be reachable from where:

| From | To | Why |
|---|---|---|
| DIAL worker/web | venue Asterisk ARI (`8088`) and AMI (`5038`) | originate, resync, `dialplan reload`, status |
| DIAL worker | venue OMM AXI (`12622`) | subscriptions, RFP sync, handset renumbering |
| venue Asterisk | DIAL `DIAL_PUBLIC_URL` over HTTPS | hooks (`/api/v1/pbx/hooks/…`), route lookups, phone provisioning |
| venue Asterisk | DIAL's PostgreSQL (`5432`) | **realtime**: endpoints, AORs, auth and the dialplan are rows DIAL writes and Asterisk reads (*shared database* mode only) |
| desk phones / OMM at the venue | DIAL over HTTPS, optionally the `ldap` service (`3890`) | remote phonebook (XML / LDAP), phone provisioning |

In practice that means a VPN (WireGuard works well) between the venue and the DIAL server, and the venue
keeps working only while that link is up - see *Known limitations* in the architecture doc. Hosting a
PostgreSQL read replica or a full DIAL copy at the venue is the mitigation for events that cannot afford
the dependency. The lighter alternative is the **venue agent** (section 9, *Provisioning mode*): the
venue Asterisk reads a local PostgreSQL that a small agent fills from DIAL over HTTPS, so the database
row above disappears and the venue keeps its last configuration when the uplink drops.

**Single box with Compose** (recommended for the central server and for a one-event lab): `cp .env.example .env`,
set `SECRET_KEY`, `POSTGRES_PASSWORD`,
`ARI_PASSWORD`/`DIAL_PBX_HOOK_SECRET`, `DIAL_PUBLIC_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`,
then `docker compose up --build -d`. Put a TLS reverse proxy in front of `web:8000` and set
`SESSION_COOKIE_SECURE=1`, `CSRF_COOKIE_SECURE=1`. The bundled `asterisk` service is the *server default*
PBX; a venue runs the same image (`deploy/asterisk`) on its own box pointed at DIAL's database. On the
event LAN switch the `asterisk` service to `network_mode: host` (SIP/RTP through Docker NAT is painful)
or set `EXTERNAL_IP`.

**Ansible**: `deploy/ansible` installs Docker, checks out DIAL and renders `.env` from inventory
variables - see [`deploy/ansible/README.md`](../deploy/ansible/README.md).

**Offline install**: on a connected machine run `docker compose build` and `docker compose pull db redis`,
then `docker save dial:local dial-asterisk:local postgres:16-alpine redis:7-alpine | gzip > dial-images.tgz`.
At the venue `docker load < dial-images.tgz` and start the stack. Nothing in DIAL needs internet at runtime
(set `EMAIL_URL=consolemail://` or point it at a local relay; leave `NTFY_URL` empty or run a local ntfy).
Set `DIAL_SEED_DEMO=0` for a real event.

### Configuration reference (recent additions)

The full list of variables with comments is in `.env.example`; these are the ones added with the
GURU3-parity features:

| Variable / setting | Default | Meaning |
|---|---|---|
| `DIAL_REQUIRE_EMAIL_VERIFICATION` | `1` | `1` = e-mail-first signup: the user enters an address, receives a link, then picks nickname/password (the account only exists after the click). Accounts that are still unverified (e.g. created before the switch, or via the API/admin) see a banner on every page and **cannot register extensions** until they confirm; orga/admin flows are never gated. `0` = one-step signup followed by a verification mail; unverified accounts only show a badge and a resend button. |
| `DIAL_EMAIL_TOKEN_TTL_HOURS` | `48` | Lifetime of registration, verification and e-mail-change links. |
| `DIAL_SPAM_GUARD` | `1` | Honeypot + time-trap on signup, login and password reset (see *Abuse protection* below). |
| `DIAL_SPAM_GUARD_MIN_SECONDS` | `3` | Signup/reset forms submitted faster than this after being rendered are rejected ("That was quick"). Login only uses the honeypot. |
| `DIAL_LOGIN_MAX_FAILURES` / `DIAL_LOGIN_IP_MAX_FAILURES` | `5` / `30` | Failed logins per account / per client IP within 15 minutes before a lockout. |
| `DIAL_LOGIN_LOCKOUT_MINUTES` | `15` | Lockout duration; locked attempts answer `429` and never say whether the account exists. |
| `DIAL_SIGNUP_BLOCKED_DOMAINS` / `DIAL_SIGNUP_ALLOWED_DOMAINS` | empty | Comma-separated e-mail domains (subdomains included) that are refused / exclusively accepted at signup and e-mail change. |
| `DIAL_PBX_OUTBOX_SYNC` | unset (follows `CELERY_TASK_ALWAYS_EAGER`) | `true`/`false`: deliver PBX outbox jobs synchronously inside the request instead of via worker/beat. Leave unset in production (a worker + beat drain the queue); set `true` only for single-process setups without Celery. |
| `DIAL_PBX_BACKEND` / `DIAL_DECT_BACKEND` + `ASTERISK_*` / `OMM_*` | dummy simulators | The **server default** adapters. Used by events that have no connection of their own on `/e/<slug>/pbx/`, and by code paths that run outside an event. |
| `DIAL_PBX_BACKENDS` / `DIAL_DECT_BACKENDS` | `asterisk`, `dummy` / `omm`, `dummy` | Adapter catalogue the per-event connection form offers (key → dotted class path). Extend in a settings module to add another PBX or DECT system. |
| `DIAL_RECORDING_DIR` | `/var/spool/asterisk/dial-recordings` | Directory where the venue Asterisk writes announcements recorded by phone (`record-announcement` service) and from which DIAL imports them into `MEDIA_ROOT/ivr/<slug>/<number>/`. Must be a volume shared between the DIAL web/worker containers and Asterisk (`recordings` in `docker-compose.yml`); the Asterisk side can override its write path with `DIAL_RECORD_DIR`. |
| `DIAL_OIDC_ENABLED` | `0` | Offer OpenID Connect login (section 3, *Single sign-on*). |
| `DIAL_OIDC_ISSUER` / `DIAL_OIDC_CLIENT_ID` / `DIAL_OIDC_CLIENT_SECRET` | empty | Issuer URL (discovery at `<issuer>/.well-known/openid-configuration`), client id and client secret. Empty secret = public client, PKCE only. |
| `DIAL_OIDC_SCOPES` | `openid email profile` | Scopes requested from the identity provider. |
| `DIAL_OIDC_BUTTON_LABEL` | `Log in with SSO` | Label of the SSO button on the login and signup pages. |
| `DIAL_OIDC_AUTO_CREATE` | `1` | Create a DIAL account on the first SSO login. `0` = only pre-existing / linked accounts may log in via SSO. |
| `DIAL_OIDC_TRUST_EMAIL_VERIFIED` | `1` | Accept the provider's `email_verified` claim: allows linking an existing account by address and skips DIAL's own e-mail confirmation for verified addresses. |
| `DIAL_OIDC_ALLOW_PASSWORD_LOGIN` | `1` | `0` = SSO only: password form, signup and reset links are hidden (also the header *Sign up* button); superusers still reach `/admin/login/`. |
| `DIAL_OIDC_USERNAME_CLAIM` | `preferred_username` | Claim used as nickname for auto-created accounts (fallback: local part of the e-mail address; duplicates get `-2`, `-3`, …). |
| `DIAL_OIDC_LOGOUT_AT_IDP` | `0` | Also end the provider session on logout (RP-initiated logout via `end_session_endpoint`). |
| `DIAL_LDAP_HOST` / `DIAL_LDAP_PORT` | `0.0.0.0` / `3890` | Bind address and port of the read-only LDAP phonebook server (`manage.py dial_ldap`, compose service `ldap`; section 8, *LDAP phonebook*). The host shown to orga in the UI comes from `DIAL_PUBLIC_URL` when the bind address is a wildcard. |
| `DIAL_LDAP_ALLOW_ANONYMOUS` | `0` | `1` = unauthenticated binds and searches see **every** servable event. LAN/VPN only. |
| `DIAL_LDAP_CACHE_SECONDS` | `30` | Entry cache of the LDAP server: phonebook edits and a rotated directory token take up to this long to show over LDAP. |
| `DIAL_PWA_ENABLED` | `1` | Serve the web-app manifest and service worker (installable, offline-capable portal - see *Progressive web app* below). `0` removes the manifest link, stops registration and answers 404 on `/sw.js`, which makes already-installed workers unregister themselves. |
| `DIAL_PWA_THEME_COLOR` / `DIAL_PWA_BACKGROUND_COLOR` | `#3b82f6` / `#0e1015` | Manifest colours (splash screen, task switcher). On event pages the browser theme colour follows the event's primary colour. |

Related per-event settings live on the event, not in the environment: `has_gsm` / `gsm_trunk`
(section 8), `NumberPlan.prefix_free` (section 4), and the **venue connection** (PBX + DECT adapters,
credentials, hook secret) on `/e/<slug>/pbx/`.

### Progressive web app (PWA)

The portal is installable ("Add to Home screen") and keeps a few read-only pages available offline.

- Service workers need **HTTPS** (or `localhost`). Behind plain HTTP everything still works, but nothing
  is installable or cached.
- What gets cached: static assets (versioned, cache-first) and only these HTML pages after a successful
  visit: `/`, `/offline/`, `/docs/…`, `/e/<slug>/` (dashboard) and `/e/<slug>/phonebook/`. Navigation
  is network-first with a 4 s timeout, then cache, then the offline page.
- What is **never** cached: `/accounts/`, `/admin/`, `/api/`, `/prov/`, `/switch/`,
  `/e/<slug>/orga/`, `/e/<slug>/pbx/`, QR codes, vCard/CSV/PDF/LDIF/XML/JSON exports, redirects,
  non-200 responses and anything sent with `Cache-Control: no-store`. A logout purges the page cache.
- Cache invalidation is automatic: the cache name is `ASSET_VERSION` (newest mtime of `dial.css`,
  `dial.js`, `pwa.css`, `pwa.js`, `sw.js`); `/sw.js` is served with `Cache-Control: no-cache`, so every
  deploy yields a new worker that drops old caches on activation and shows users a "DIAL was updated"
  toast. Icons live in `static/icons/` (`scripts/make_icons.py` regenerates them).
- No Web Push and no background sync - notifications still go by e-mail/webhook/ntfy.

### Abuse protection on signup, login and password reset

DIAL ships without a captcha or any third-party service; the layers below stop the usual bot traffic
and credential stuffing and are all on by default:

- **Honeypot + time trap** (`DIAL_SPAM_GUARD`). Public forms carry a hidden `website` field that humans
  never see (off-screen via CSS, `aria-hidden`) and a signed render timestamp. Bots that fill every
  field or submit within `DIAL_SPAM_GUARD_MIN_SECONDS` get a generic *Spam protection triggered* error.
  Login uses the honeypot only, so password managers are not penalised.
- **Login lockout.** After `DIAL_LOGIN_MAX_FAILURES` wrong passwords for one account (or
  `DIAL_LOGIN_IP_MAX_FAILURES` from one IP) within 15 minutes, the login page answers `429` for
  `DIAL_LOGIN_LOCKOUT_MINUTES` - also for the correct password, and identically for unknown accounts.
  An audit entry (`login`, no actor, target = the account) is written; the `dial.security` logger
  records every lock. A successful login resets the account counter. To unlock early, flush the
  cache (`manage.py shell -c "from django.core.cache import cache; cache.clear()"`) or wait.
- **Per-IP rate limits** (`DIAL_RATE_LIMITS`, `RateLimitMiddleware`) on `/accounts/login/`,
  `/accounts/register/`, `/accounts/password/reset/` and the availability API; verification mails are
  additionally capped at 5 per address and 20 per IP per hour.
- **Domain lists.** `DIAL_SIGNUP_BLOCKED_DOMAINS` for throwaway providers, or
  `DIAL_SIGNUP_ALLOWED_DOMAINS` to restrict the server to your organisation's domains.
- **Forced e-mail verification** (`DIAL_REQUIRE_EMAIL_VERIFICATION=1`, default). No account exists
  before the link is clicked, and unverified accounts cannot register extensions.

Behind a reverse proxy DIAL takes the client address from the first `X-Forwarded-For` entry, so make
sure the proxy sets (and overwrites) that header; otherwise lockouts and rate limits would count the
proxy as one client. A shared venue NAT may need higher `DIAL_LOGIN_IP_MAX_FAILURES` / `DIAL_RATE_LIMITS`.

### Early access (public server, not public yet)

Set `DIAL_EARLY_ACCESS_PASSWORD` before the server is reachable from the internet. Every browser then has
to enter that password once (valid `DIAL_EARLY_ACCESS_DAYS`, default 30) before it sees any page - login
and signup included. Share it with your team; change it to lock everyone out again; remove it to go
public. `DIAL_EARLY_ACCESS_MESSAGE` replaces the text on the gate page.

Machines keep working because they authenticate with their own secrets: phone provisioning (`/prov/`),
the remote phonebook XML for desk phones and the OMM, PBX hooks and the venue agent (`X-DIAL-PBX-Secret`),
API clients with a `dial_` service token, the federation directory, the PWA manifest/service worker and
static files. The LDAP phonebook server is a separate process and unaffected. EVAC implements the same
gate (`EVAC_EARLY_ACCESS_*`, EVAC ADR-0012).

## 3. Initial setup

1. Create the admin: with `DIAL_SEED_DEMO=0` run `docker compose exec web python manage.py createsuperuser`.
2. Seed the shared lookup tables (idempotent, safe to re-run after upgrades):
   - `manage.py dial_provisioning_profiles` - builtin autoprovisioning templates for Snom, Yealink,
     Grandstream and Cisco SPA (section 8);
   - `manage.py dial_dect_vendors` - a small best-effort list of DECT manufacturer codes (EMC, first 5
     IPEI digits) for the handset registry (section 7). Verify codes before relying on them; users and
     orga can extend the list in the UI.
   - `manage.py dial_purge_tokens` prunes expired e-mail confirmation tokens; the beat task
     `accounts-purge-email-tokens` does the same daily, so you only need the command without beat.
3. Log in, go to `/events/new/` and create the event (name, slug, dates, timezone, location, SIP domain).
   Your account becomes event admin. Only global admins can create events; hand the event over by
   giving the orga role on `/e/<slug>/orga/members/` - see the [Event Guide](EVENT_GUIDE.md).
4. Branding: `/e/<slug>/orga/settings/` - logo, primary/accent colour, announcement text shown on the
   dashboard, default announcement language (Asterisk sound pack), `max_extensions_per_user`, guest extensions, breakout, CDR privacy,
   GSM (*has GSM* + trunk name).
5. Lifecycle (`/e/<slug>/orga/state/<state>/` or `dial events transition <slug> <state>`):

| State | Unlocks |
|---|---|
| `draft` | only orga sees the event; design plan, groups, import |
| `registration` | users can join and request numbers; DECT polling runs; provisioning happens |
| `live` | same as registration, plus the event is highlighted; use it while on site |
| `archived` | read-only, browsable phonebook/stats; no new registrations, no polling |

Transitions can be **scheduled**: the *Schedule* group on the settings page (`registration_opens_at`,
`goes_live_at`, `archives_at`, entered in the event timezone; `dial events schedule <slug> --live ISO …`)
is applied by the beat task `events-apply-scheduled-transitions` once a minute, one lifecycle step at a
time, and the orga dashboard shows *Next: …* until then. Needs a running `beat` container.

### Single sign-on (OpenID Connect)

DIAL can log users in through any OpenID Connect provider - Keycloak, Authentik, Zitadel, … - using the
authorization-code flow with PKCE. Nothing extra to install; it is part of the core and off by default
(`DIAL_OIDC_ENABLED=0`).

**At the identity provider**

1. Create an OIDC client (Keycloak: *Clients → Create client*, type OpenID Connect, *client
   authentication* on; Authentik: *Providers → OAuth2/OpenID Provider*, confidential).
2. Redirect URI `https://<host>/accounts/oidc/callback/` - with the trailing slash.
3. Post-logout redirect URI `https://<host>/` - only needed with `DIAL_OIDC_LOGOUT_AT_IDP=1`.
4. Grant type *authorization code*, PKCE method `S256`, scopes `openid email profile`.
5. Release the claims `email`, `email_verified` and `preferred_username`.
6. Copy client id, client secret and the issuer URL (Keycloak `https://id.example.org/realms/<realm>`,
   Authentik `https://id.example.org/application/o/<slug>/`).

**In DIAL's `.env`**

```
DIAL_OIDC_ENABLED=1
DIAL_OIDC_ISSUER=https://id.example.org/realms/dial
DIAL_OIDC_CLIENT_ID=dial
DIAL_OIDC_CLIENT_SECRET=...          # empty = public client, PKCE only
# optional
DIAL_OIDC_ALLOW_PASSWORD_LOGIN=0     # SSO only
DIAL_OIDC_AUTO_CREATE=0              # only existing / linked accounts may log in
DIAL_OIDC_LOGOUT_AT_IDP=1            # also end the provider session on logout
```

Then run `manage.py dial_oidc_check`: it fetches the discovery document (bypassing the cache), prints
the endpoints DIAL will use and the redirect URI to register, and warns about missing claims and
incomplete settings (exit code non-zero). The login page now shows a button labelled
`DIAL_OIDC_BUTTON_LABEL`; with `DIAL_OIDC_ALLOW_PASSWORD_LOGIN=0` the password form, the signup form,
the reset link and the header *Sign up* button disappear - superusers still reach `/admin/login/`. All
variables are listed in the *Configuration reference* (section 2).

**How accounts are matched.** Returning users are recognised by the provider's stable subject, stored
as `User.oidc_subject` (`<issuer>|<sub>`). On the first SSO login DIAL links an **existing account with
the same e-mail address only if that address is verified** - by DIAL, or by the provider's
`email_verified` claim when `DIAL_OIDC_TRUST_EMAIL_VERIFIED=1`. An unverified match is refused with
"verify your e-mail address and try again" (section 19). Otherwise a new account is created
(`DIAL_OIDC_AUTO_CREATE=1`) with an unusable password and the nickname from `DIAL_OIDC_USERNAME_CLAIM`
(deduplicated as `name-2`, `name-3`, …). Users link or unlink SSO themselves on the profile page;
unlinking requires a usable password so nobody locks themselves out. Failed callbacks count toward the
per-IP login lockout like wrong passwords (*Abuse protection*, section 2).

**Limitations.** No ID-token signature verification - by design: the token comes straight from the
token endpoint over TLS (OIDC Core §3.1.3.7) and `iss`, `aud`/`azp`, `exp`, `iat` and `nonce` are
checked instead. No back-channel logout and no refresh tokens; e-mail address and nickname are not
re-synced on later logins; the discovery document is cached for one hour, so changes at the provider
take up to that long (or a cache flush) to show. Background in the architecture doc, sections 7 and 11.

### Where things are in the UI

Inside an event, orga members get orange **Orga** groups in the left sidebar; every page below lives
there, so you rarely need the URLs:

| Sidebar group | Pages |
|---|---|
| (user section) | Overview, Phonebook (orga additionally see **Settings**: intro text, categories, **Remote directory for phones** with the per-vendor XML URLs, LDAP details and *Rotate token*), **Info pages** (`/e/<slug>/pages/`), Register, My extensions (extension detail: *Business card* with vCard/QR/print, *Shown on handset as*), My handsets (SIP device page: softphone QR per client + *Manual settings*), Call groups, Callbacks & wake-up, Voicemail, Announcements & IVR (*Record by phone* card on an announcement) |
| (account menu) | Profile (`/accounts/profile/`): display name, password, API tokens, data export - and, when SSO is enabled, a **Single sign-on** card to link or unlink the identity |
| Orga · Numbers | Dashboard (state buttons, *Next:* scheduled transition), Approval queue (badge = pending requests; trunk blocks always land here), All extensions, **Import CSV** (`/e/<slug>/orga/import-csv/`, upload → preview → apply), Number plan (ranges + numbering settings, service numbers, feature codes), Pools, Claims, Guest numbers |
| Orga · People | Members, User groups, Helpdesk lookup (search by digits shows the **number history** card) |
| Orga · Infrastructure | **PBX & DECT connection** (the event's venue Asterisk/OMM, **provisioning mode** shared database / venue agent, test, env snippet, **Venue agent** card with online/stale, up to date/behind and the agent's environment block), DECT (infrastructure, handsets, coverage map, alerts, site survey), Handset vendors, GSM, Statistics |
| Orga · Services | Callbacks, Voicemail, Broadcast, Emergency, Federation, Breakout |
| Orga · Event | Settings (state, **Schedule**, clone, export/import), **Info pages** (`/e/<slug>/pages/manage/`), Webhooks, API tokens, Audit log |

Helpdesk-only members see a smaller *Helpdesk* group (user lookup with number history, statistics).

## 4. Designing the number plan (worked example)

`/e/<slug>/orga/numberplan/`. Plan-level settings: `min_length`/`max_length`, defaults for numbers no range
matches (`default_allowed`, `default_requires_approval`), service numbers, emergency numbers, feature codes.
Ranges match by prefix and/or regex, **lower priority wins**.

A 4-digit camp plan (this is what `seed_demo` creates):

| Priority | Name | Match | Mode | Extras |
|---|---|---|---|---|
| 1 | Trunk prefix | prefix `0` | blocked | reserved for breakout |
| 1 | Services | prefix `9` | blocked | 9000 test ringback, 9001 wake-up, 9002 site survey, 9003 echo, 9999 voicemail |
| 5 | Vanity | pattern `(\d)\1{3}` | open + `is_vanity` | 7777 etc. always need approval |
| 10 | Orga | prefix `1` | restricted | `allowed_roles = [orga, admin]` |
| 20 | Angels | prefix `2` | restricted | `allowed_groups = [angels]` |
| 30 | Premium | prefix `8` | approval | reviewed by orga |
| - | (default) | anything else 4 digits | open | quota from `max_extensions_per_user` (3) |

Emergency numbers `112`, `110` are set on the plan and cannot be registered; routing is configured in
`/e/<slug>/emergency/`. Per-range `quota_per_user` overrides the event quota inside that range.

Feature codes live on the plan too (form section *Feature codes*): `*66`/`*86` callbacks, `*71`/`*72`
group login/logout, and call forwarding from the handset - `*21<n>` always, `*22<n>` busy, `*23<n>` no
answer, `*20` off (`forward_set_code` & co.; blank = off). The **announcement recording number**
(`announcement_record_number`, e.g. `9005`) lets announcement owners record by phone (section 9).
Digit-only codes and service numbers count as reserved numbers for prefix-free checks and trunk blocks.

Use the **"test a number"** box on the plan page (it calls `/api/v1/availability/`) to verify the outcome
for the currently logged-in user - it tells you the matched range, whether approval is required and why a
number is denied.

*Screenshot placeholder: number plan page with ranges table and test box.*

### Prefix-free numbering

`NumberPlan.prefix_free` (default **on**; toggle under *Number plan* or at `/e/<slug>/numbering/settings/`)
enforces that no two extensions are prefixes of each other: `23` blocks `2323` and vice versa. The
benefit is that every number dials without an inter-digit timeout - the PBX knows the number is
complete as soon as the last digit arrives. Digit-only service numbers, emergency numbers and feature
codes count as taken prefixes too. The live checker and the register form name the conflicting number
(`conflicts` list in the `/api/v1/availability/` response). Orga may register numbers outside the
plan's length range, but never a prefix conflict. Switch it off only if your plan mixes lengths on
purpose (e.g. 3-digit team numbers under 4-digit ranges) and you accept the dial timeout.

### Random numbers & extension pools

The registration form has a **🎲 Give me a random free number** button that fills in an instantly
registrable number. Define **extension pools** at `/e/<slug>/numbering/pools/` (name, prefix, total
length, active flag) to control where random numbers come from - e.g. a pool `4` / length 4 for
attendees. Without pools DIAL samples numbers in the plan's default length range. The same lookup is
available as `GET /api/v1/random-number/?event=<slug>&type=<type>` (badge printers, kiosks).

### Extension claims (reserving a number for a person)

At `/e/<slug>/numbering/claims/` you reserve a number for one specific person - by account or, for
people without an account yet, by e-mail address. DIAL sends an invite link
(`/e/<slug>/numbering/claim/<token>/`); while the claim is open the number is blocked for everybody
else (including prefix conflicts). The invitee logs in, confirms, and the extension is active
immediately - claims **override** range policy (restricted/approval ranges), but never emergency,
service, blocked or already-taken numbers. Claims expire after 14 days by default (`valid_until`), can
be re-sent (`/claims/<id>/resend/`) or deleted, and redeemed claims link to the resulting extension.
Typical use: infodesk, team leads, sponsors' booth numbers. A claim reserves the number **within this
event only** - numbers are per event and nobody can hold one permanently; porting (section 6) is a
re-request under the new event's plan.

### SIP trunk blocks (`4700–4799` to a remote PBX)

A *SIP trunk* extension routes a whole number block to a remote PBX (village, sponsor booth, partner
network). The base must end in as many zeros as the block has wildcard digits (`block_digits` 1/2/3 →
10/100/1000 numbers; `4700` + 2 = `4700–4799`). Policy: the whole block is evaluated against the plan
(base, last number, no reserved number inside, no blocked/restricted range starting inside), overlap is
refused in both directions (block vs. single number), and **trunk requests always need approval** unless
orga registers them (`apps/numbering/blocks.py::evaluate_block`). Exactly one SIP device is attached per
trunk; the phonebook shows `4700–4799`. Checker: `/api/v1/availability/?event=&number=4700&type=trunk&block_digits=2`;
CLI: `dial extensions create --type trunk --block-digits 2 …`. Dialplan and caller-ID limitation: section 8.

## 5. Groups & roles

| Role | Can |
|---|---|
| user | join event, register/edit/delete own extensions and devices, phonebook, callbacks, voicemail, groups they are a member of |
| helpdesk | everything a user can + `/e/<slug>/orga/helpdesk/` lookup by number/e-mail/IPEI, view any extension/device, reissue PINs (audit-logged as `impersonate`) |
| orga / admin (event) | + event settings, state transitions, moderation queue, number plan, members & roles, groups, guests, webhooks, service tokens, resync, export, DECT admin, stats. *Event admin* has the same rights as orga; it only marks the event's owners. |
| global admin (superuser) | implicit event admin everywhere + **create, clone, import and delete events** |

Roles are **per event**: assign them at `/e/<slug>/orga/members/` (nickname or e-mail of an existing
account). Step-by-step instructions for creating, staffing and running an event are in the
[Event Guide](EVENT_GUIDE.md). Create `UserGroup`s (with optional `join_code` users can enter on the
join page) at `/e/<slug>/orga/groups/`. Groups gate restricted ranges and can back call groups and
breakout permissions.

## 6. Porting from last year

- **Clone event** (global admin only): `/e/<slug>/orga/clone/` (or `POST /api/v1/events/<slug>/clone/`)
  copies settings, number plan and ranges (group references are re-linked by slug), groups and the
  orga/admin team into a new draft event.
- **Users re-request numbers**: on the new event's dashboard users see "port your numbers" listing
  extensions they held in previous events (`/e/<slug>/extensions/port/`); policy is re-evaluated.
  Numbers are **per event**: porting is a convenience re-request through the new plan, never a right to
  a number, and orga claims only reserve within one event.
- **Export / import JSON**: `/e/<slug>/orga/export/` produces a full event dump (plan, ranges, groups,
  extensions, devices, groups, mailboxes...), `/e/<slug>/orga/import/` restores it into an event.

## 7. DECT setup

Prerequisites: a Mitel SIP-DECT OMM reachable from the DIAL worker (over the venue VPN), an AXI user,
RFPs licensed and synced, the OMM registering its users to the venue Asterisk (SIP domain = the event's
`sip_domain`).

Connect it on `/e/<slug>/pbx/` → *DECT connection*: backend **Mitel SIP-DECT OMM**, host, port (12622),
AXI user and password, *verify TLS* off for self-signed OMMs. *Test connection* shows OMM version and
RFP count. Events without a DECT connection use the server default:

```
DIAL_DECT_BACKEND=apps.dect.backends.omm.MitelOMM
OMM_HOST=10.0.0.5  OMM_PORT=12622  OMM_USER=omm  OMM_PASSWORD=...  OMM_VERIFY_TLS=0
```

- **Subscription mode**: when a DECT device is provisioned DIAL creates the PP device + user on the OMM and
  calls `open_subscription_window(30)`. The user enters the PIN on the handset; PIN validity is 30 minutes
  (`Device.issue_subscription_pin(ttl_minutes=30)`), helpdesk can reissue.
- **Dial-to-claim (GURU3-style handset binding)**: set a **DECT claim number** in the number plan (9004 in
  the demo plan). Every DECT extension then carries a 6-digit *claim code* (`Extension.dect_claim_code`,
  length via `DIAL_DECT_CLAIM_CODE_LENGTH`); the extension page shows `<claim number><code>`. A handset
  dialling that from the event context hits `[dial-services] dect-claim`, which POSTs the `dect-claim` hook
  with the caller's PJSIP endpoint; `apps.dect.claim.claim_handset` binds the device, renumbers it via
  `update_subscription` and the dialplan announces the new number. Handsets get into the **claim pool**
  (`Device.unclaimed=True`, temp number `<claim number>NNN`, display name *Dial 9004+code*) in two ways:
  - *Pool*: the orga adds an IPEI on `/e/<slug>/dect/handsets/` ("Add pool handset"); DIAL creates the
    subscription with a PIN like for any device. Works with the dummy backend for demos.
  - *Adoption*: handsets the OMM already knows but DIAL does not (e.g. OMM **auto-create on subscription**
    with an event-wide AC) are adopted on the next `sync_infrastructure` run: DIAL gives them its own SIP
    identity (`SetPPUser sipAuthId/sipPw`, or `CreatePPUser` when the PP has no user) and an Asterisk
    endpoint. Adoption only happens while a claim number is configured; otherwise unknown IPEIs are ignored
    as before. Both OMM code paths are **not yet validated on hardware** - test with your firmware.
  Dialling another extension's code moves the handset (old bindings are dropped and both extensions are
  re-provisioned); everything is audited and emitted as `device.claimed` / `device.adopted` webhooks.
- **Dashboard** `/e/<slug>/dect/`: RFP table (connected/synced/cluster/active calls), cluster health,
  handset list (`/handsets/`), open alerts, "Sync now" button (`/sync/`, or `dial dect sync --event <slug>`).
- **Coverage map**: upload a floor plan under `/e/<slug>/dect/map/`, then click to place RFPs
  (`/map/place/`). Handsets are drawn at their `last_seen_rfp`; RFPs without any handset for a while are
  flagged as weak zones (`weak_zones()`).
- **Site survey**: dial the survey number (9002 in the demo plan) from a handset while walking; Asterisk
  loops through the `site-survey` hook and reads out the serving RFP name; each hit is logged in
  `SiteSurveyLog` (`/e/<slug>/dect/survey/`).
- **Alerting**: set `ALERT_WEBHOOK_URL`, `NTFY_URL` and/or `ALERT_EMAILS`. Kinds: `rfp.down`, `rfp.up`,
  `sync.degraded`, `omm.unreachable`. Webhook subscribers get `dect.rfp.down` etc.
- **Handset registry**: DIAL recognises the manufacturer from the IPEI's first 5 digits (EMC) via the
  shared `DECTManufacturer` table. `manage.py dial_dect_vendors` loads a small builtin list - it is
  best-effort, verify codes before relying on them. Unknown vendors can be suggested by users on the
  device page (`/e/<slug>/devices/vendor/<id>/`); curate suggestions at
  `/e/<slug>/devices/manufacturers/`. The table is server-wide, not per event.
- **Handset history**: users see every handset they used across events at `/e/<slug>/devices/history/`
  and can **Reuse in this event** (copies IPEI, model, label and the OMM `Device.uak` so the handset can
  re-subscribe without a new PIN where the OMM supports it).
- **DECT encryption**: the per-extension toggle is passed to the OMM as `encrypt` on the PP user. It
  has not been validated against real hardware - test it with your firmware before announcing it.

## 8. SIP onboarding

Each SIP device gets a 24-character password and a QR code (`/e/<slug>/devices/<id>/qr.png`) encoding a
`sip:user:pass@domain;transport=udp` deep link for softphones.

### Softphone QR provisioning

The SIP device page offers one `<details>` accordion per client family plus a *Manual settings* card:

| Client | QR / URL | Notes |
|---|---|---|
| Generic (`sip:` URI) | `/e/<slug>/devices/<id>/qr.png` | deep link with credentials; Grandstream Wave and friends |
| Linphone | `?client=linphone` → `linphone-config:https://<dial>/prov/<token>/linphone.xml` | remote-provisioning XML |
| Acrobits / Groundwire | `?client=acrobits` → `https://<dial>/prov/<token>/acrobits.xml` | `<account>` XML, scanned from the app's *Scan QR code* |

`Device.softphone_links()` (also `softphone_links` in the device API when the secret is requested) returns
all three. The `/prov/<token>/…` URLs serve credentials without further login - treat them like the
password. The XML documents follow the vendors' published formats but have **not been verified on the
real apps** yet; test with your softphone before printing QR codes on badges.

### SIP trunks (number blocks)

A trunk extension (section 4) has exactly **one SIP device**; the remote PBX registers with that account.
DIAL writes two dialplan rows per trunk: the base (`4700`) and an Asterisk pattern for the block
(`_47XX`), both dialling `PJSIP/${EXTEN}@<sip_username>` so the number arrives unchanged at the remote
PBX; the route API answers `type: "trunk"` for any number of an active block. **Limitation**: inbound
caller-ID from the remote PBX is rewritten to the block base (`trust_id_inbound=no`), so `4711` calls
out as `4700` - details in [`deploy/asterisk/README.md`](../deploy/asterisk/README.md).

### Autoprovisioning served by DIAL

Hardphones are provisioned from DIAL itself - no separate provisioning server needed:

1. Load the builtin templates with `manage.py dial_provisioning_profiles` (Snom, Yealink, Grandstream,
   Cisco SPA) or write your own `ProvisioningProfile` in the Django admin (`/admin/`): a Django template
   with `device`, `extension`, `event`, `sip_server`, `sip_port`, `transport` in context, plus a
   `filename_pattern`.
2. Attach a profile to the device. The device page then shows the per-device URL
   `https://<dial>/prov/<token>/<filename>` - **treat it as a password**, it serves the SIP credentials
   without further authentication (the token is the secret).
3. Phones that only know their MAC can fetch `GET /prov/<vendor>/<mac>.cfg|.xml` (vendor is one of
   `snom`, `yealink`, `grandstream`, `cisco`, `generic`). This route requires `?token=<provisioning
   token>` or HTTP Basic auth with `sip_username:sip_password`; DIAL never serves credentials on the MAC
   alone and answers `401` with a help text otherwise.

| Vendor | Filename pattern |
|---|---|
| Snom | `{mac}.xml` |
| Yealink | `{mac}.cfg` |
| Grandstream | `cfg{mac}.xml` |
| Cisco SPA | `spa{mac}.cfg` |

Put DIAL behind TLS for this; responses carry `Cache-Control: no-store`.

### Remote phonebook (XML) for desk phones & OMM

Desk phones and the DECT OMM can show the event phonebook **on the device** (the user searches on the
handset, the phone queries DIAL). Phones cannot log in, so every event carries a **directory token** -
*Phonebook → Settings → Remote directory for phones* (`/e/<slug>/phonebook/settings/`, orga only). That
token is the credential: a path segment of the XML URLs below and the bind password of the LDAP server
(next section). The settings page lists one URL per vendor:

```
https://<dial>/e/<slug>/phonebook/remote/<token>/<vendor>.xml
                                                  vendor = snom | yealink | grandstream | cisco | mitel | generic
```

Append `?q=<fragment>` to filter - handsets do that when the user types a name (`?search=` and `?name=`
are accepted too, which is what the OMM sends). Served are the active extensions with *Public phonebook
entry*; trunk blocks appear with the dialable base number and the range in the name
(`Foo PBX (4700–4799)`).

- **Rotate** the token on that page, with `dial phonebook directory --event <slug> --rotate` or
  `POST /api/v1/phonebook/directory/rotate/?event=<slug>` to lock out **every** phone, OMM and LDAP
  client at once - each needs the new URL / password afterwards. Untick **Remote directory enabled** to
  switch the whole thing off (URLs answer `404`, LDAP binds fail). The token is never part of an export
  or clone; a new event gets a fresh one.
- **Autoprovisioned phones need nothing extra.** The built-in Snom, Yealink, Grandstream and Cisco SPA
  profiles point the phone at `/prov/<its own provisioning token>/phonebook.xml`, so the event-wide
  token never leaves the server (the vendor format follows the device's profile, `?vendor=` overrides).
  Existing installations refresh the built-in templates with `manage.py dial_provisioning_profiles --update`;
  custom templates get the URL as `phonebook_url` in their context.
- **Manual setup**, if the phone is not provisioned by DIAL:

| Phone / system | Where | Value |
|---|---|---|
| Snom | *Function keys → Directory key*, type `url` (setting `dkey_directory`) | the `snom` URL |
| Yealink | *Directory → Remote Phone Book → URL* (`remote_phonebook.data.1.url`) | the `yealink` URL |
| Grandstream | *Phonebook → XML phonebook*: enable HTTP/HTTPS download, server path **without** `/phonebook.xml` - the phone appends it (P330/P331/P332) | the `grandstream` URL minus the file name |
| Cisco SPA | *XML Directory Service URL* | the `cisco` URL |
| Mitel SIP-DECT OMM | *System → XML applications → Corporate directory*, URL | the `mitel` URL; the OMM appends the search string itself |
| Anything speaking `<IPPhoneDirectory>` | the vendor's remote-directory URL field | the `generic` URL |

The URL contains a secret - serve it over TLS only. Scripted access: `GET /api/v1/phonebook/directory/?event=<slug>`
returns URLs plus LDAP details, `dial phonebook directory --event <slug>` prints the same.

### LDAP phonebook

For phones and OMMs that prefer a directory server, DIAL serves every event's phonebook **read-only over
LDAP v3**. The server is `manage.py dial_ldap` - in Compose the service `ldap`, published on port `3890`
(`DIAL_LDAP_PORT`); append `--cert fullchain-and-key.pem` to its command for LDAPS. Only events in state
*registration* or *live* with the remote directory enabled are served; the bind password is the
directory token from the previous section, so a rotation locks LDAP clients out too.

| Setting | Value |
|---|---|
| Server / port | `<dial-host>` / `3890` (plain LDAP; LDAPS only with `--cert`) |
| Base DN | `ou=phonebook,dc=<slug>,dc=dial` |
| Bind DN | `cn=directory,dc=<slug>,dc=dial` |
| Password | the event's directory token |
| Protocol | version 3, simple bind, search scope *sub* |
| Name attributes | `cn` (also `sn`, `givenName`, `displayName`) |
| Number attributes | `telephoneNumber` (`mobile` carries the same number) |
| Display | `%cn` |

The name filter is an OR over `cn` and `sn` with a trailing wildcard, the number filter an OR over
`telephoneNumber` and `mobile`. Typed out for the two most common phones:

```
# Yealink (Directory -> LDAP, or in the .cfg)
ldap.enable = 1
ldap.version = 3
ldap.host = <dial-host>
ldap.port = 3890
ldap.base = ou=phonebook,dc=<slug>,dc=dial
ldap.user = cn=directory,dc=<slug>,dc=dial
ldap.password = <directory token>
ldap.name_filter = (|(cn=*%)(sn=*%))
ldap.number_filter = (|(telephoneNumber=%)(mobile=%))
ldap.name_attr = cn
ldap.numb_attr = telephoneNumber mobile
ldap.display_name = %cn
ldap.incoming_call_special_search = 1

# Snom (Advanced -> LDAP)
ldap_server = <dial-host>
ldap_port = 3890
ldap_base = ou=phonebook,dc=<slug>,dc=dial
ldap_username = cn=directory,dc=<slug>,dc=dial
ldap_password = <directory token>
ldap_search_filter = (|(cn=*%)(sn=*%))
ldap_number_filter = (|(telephoneNumber=%)(mobile=%))
ldap_name_attributes = cn
ldap_number_attributes = telephoneNumber mobile
ldap_display_name = %cn
```

**Mitel SIP-DECT OMM**: *System → LDAP/Directory* - server, port `3890`, search base, user and password
as above; attribute mapping name `cn`, surname `sn`, first name `givenName`, office `telephoneNumber`,
mobile `mobile`.

Limitations to know about: simple bind only (no SASL), **no StartTLS** (clients asking for it get
`protocolError` - use LDAPS via `--cert`), no paging controls, `>=`/`<=` filters never match, and
entries are cached for `DIAL_LDAP_CACHE_SECONDS` (30 s) - a rotated token or an edited entry takes up to
that long to show. `DIAL_LDAP_ALLOW_ANONYMOUS=1` lets anyone who reaches the port read **all** servable
events; keep it off outside a closed venue LAN. Only the venue needs to reach `3890` - do not publish it
to the internet without LDAPS.

### GSM handsets

If the event runs an on-site cell network (Osmocom & co.), enable **has GSM** and set the PJSIP trunk
name (`gsm_trunk`, default `gsm-gateway`) in `/e/<slug>/orga/settings/`. The GSM endpoint type is
enabled **per event**, not globally.

- Devices appear at `/e/<slug>/devices/gsm/` with IMSI, optional MSISDN and 2G/3G/4G/5G opt-ins; each
  gets a 6-digit one-time registration code (`/gsm/<id>/token/` reissues it).
- The GSM core links the SIM by calling `POST /prov/gsm/register/` with header `X-DIAL-PBX-Secret`
  (same secret as the Asterisk hooks) and body `{event, token, imsi, msisdn?}` (JSON or form-encoded).
  DIAL binds the SIM to the device holding that token and records `gsm_registered_at`.
- Extensions bound to a GSM device ring through the trunk as `PJSIP/<msisdn>@<gsm_trunk>`. A commented
  `[gsm-gateway]` endpoint/aor/identify template is in `deploy/asterisk/conf/pjsip.conf`.

## 9. Asterisk operations

Read [`deploy/asterisk/README.md`](../deploy/asterisk/README.md) once - it explains the realtime table
mapping, the dialplan and the hooks.

- **Connecting a venue Asterisk**: `/e/<slug>/pbx/` → *PBX connection*: backend **Asterisk**, ARI URL
  (`http://<venue-ip>:8088/ari`), ARI user/password/app, optional AMI host/port/user/password, a
  **hook secret** for this event and the **provisioning** mode (shared database or venue agent, see
  below). The page also prints the environment the venue box needs
  (`DIAL_API_URL`, `DIAL_EVENTS`, `DIAL_PBX_HOOK_SECRET`, `SIP_DOMAIN`; the database goes into `DB_HOST` etc.). *Test connection* calls `health()` on both adapters.
  Resetting the connection makes the event fall back to the server default. The orga dashboard shows
  a *Venue connection* card (red while PBX or DECT still run on the server default). Same thing
  scripted: `dial pbx connection show|set|reset --event <slug>` / `GET|PATCH|DELETE /api/v1/pbx/connection/?event=`;
  for support there is also *PBX connections* / *DECT connections* in the Django admin.
- **Resync**: orga button at `/e/<slug>/orga/resync/` or `POST /api/v1/pbx/resync/?event=<slug>` rewrites
  all realtime rows for the event and triggers `dialplan reload` on *that event's* Asterisk.
- **Check rows**: `docker compose exec asterisk asterisk -rx "pjsip show endpoints"`,
  `asterisk -rx "dialplan show 4242@dial-<slug>"`, or `make dbshell` → `select * from extensions where context='dial-<slug>';`.
- **Hook secret**: the venue Asterisk sends `X-DIAL-PBX-Secret` on every hook, route lookup and phone
  provisioning request. DIAL accepts the event's *hook secret* from the PBX connection if one is set,
  otherwise the server-wide `DIAL_PBX_HOOK_SECRET` (falls back to `ARI_PASSWORD`). Avoid `; $ { }` in it.
- **NAT / audio**: set `EXTERNAL_IP` to the host LAN IP when using bridge networking, keep the RTP range
  (`10000-10200/udp`) published, or use `network_mode: host`.
- **Status**: `GET /api/v1/pbx/status/?event=<slug>`, `GET /api/v1/health/?event=<slug>`, `dial health --event <slug>`.
- **Custom ringback tones**: users upload a tone on the extension; the worker converts it to 8 kHz mono
  WAV (needs `ffmpeg` on the worker for MP3/OGG/FLAC input; WAV works without). Asterisk plays it as a
  music-on-hold class `dial-<slug>-<number>` via the Dial option `m(...)`. MOH classes are **static**:
  render them with `AsteriskPBX.render_musiconhold(event)` (e.g. from `manage.py shell`), write the
  output to `/etc/asterisk/dial-moh.conf` (included by `musiconhold.conf`) and run
  `asterisk -rx "moh reload"`. Asterisk must be able to read `MEDIA_ROOT/ringback/processed/` -
  share the `media` volume with the Asterisk container.
- **Per-extension endpoint options**: call waiting off sets `device_state_busy_at=1` on the endpoint;
  the announcement language becomes `Set(CHANNEL(language)=...)` in the dialplan preamble and
  `language=` on the endpoint; caller-ID display mode changes the `callerid` column.
- **Display name changes**: renaming an extension re-pushes the name to the PBX (`callerid`) and, for
  DECT, to the OMM via `update_subscription` - the handset shows the new name **without a new
  subscription**. The extension page prints *Shown on handset as*.
- **Recording announcements by phone**: the `record-announcement` service (plan field
  *announcement recording number*) calls two hooks - `announcement-record-start` (`event, caller,
  callerid, code` → `{handled, number, name, file}`) and `announcement-recorded` (`event, code, file,
  duration`). Asterisk writes `<file>.wav` into `DIAL_RECORDING_DIR` (default
  `/var/spool/asterisk/dial-recordings`), DIAL imports it into `MEDIA_ROOT/ivr/<slug>/<number>/` and fires
  `announcement.recorded`. That directory **must be one volume** mounted in the DIAL web/worker
  containers and in Asterisk (`recordings` in `docker-compose.yml`); a venue box that cannot share it can
  set `DIAL_RECORD_DIR` in the Asterisk environment to write elsewhere, but then DIAL only keeps the path.
  The custom prompts `dial/record-*` are not shipped - Asterisk core prompts are used as fallback - and
  the dialplan has not been exercised against a real Asterisk yet.
- **Trunk blocks**: `4700` + `_47XX` rows, `Dial(PJSIP/${EXTEN}@<sip_username>)`, caller-ID rewritten
  to the base - section 8.
- **Beat tasks** the PBX side relies on: `pbx-outbox-drain` (5 s), `pbx-outbox-purge-delivered`
  (daily), `dect-poll-infrastructure` (30 s), `callback-dispatch-due` (10 s),
  `events-apply-scheduled-transitions` (60 s, scheduled lifecycle changes), `stats-aggregate-hourly`,
  `stats-enforce-retention`, `extensions-expire-guest`, `accounts-purge-email-tokens`.

### PBX outbox (provisioning queue)

Every push to the PBX (extension/device/event sync and removal, MWI, originate/broadcast) is written as
a `PBXJob` row and delivered by the worker - kicked on commit and swept by beat every 5 s
(`pbx-outbox-drain`). If the PBX is unreachable the job backs off exponentially (10 s doubling up to a
10 min cap), gives up after 5 attempts and is parked as **dead**; the affected extension shows
`PBX: <error>` in its provision status. Repeated edits to the same extension/device coalesce into one
push while the job is still open. Delivered jobs are purged after 7 days (`pbx-outbox-purge-delivered`).

Where to watch and act:

- orga dashboard widget (live counts per state);
- `GET /api/v1/pbx/outbox/?event=<slug>` (stats + the 20 most recent jobs) and
  `POST /api/v1/pbx/outbox/retry/?event=<slug>` (re-queue all dead jobs);
- `dial pbx outbox --event <slug> [--retry-dead]`;
- Django admin *PBX jobs* with **Retry** and **Deliver now** actions.

In dev/tests (Celery eager) jobs are delivered synchronously; force either mode with the Django setting
`DIAL_PBX_OUTBOX_SYNC` (see section 2). `POST /api/v1/pbx/resync/` bypasses the queue and calls the
adapter directly, which makes it a good "is the PBX reachable at all" test.

### Provisioning mode: shared database vs. venue agent

The PBX connection has a **Provisioning** field that decides how the realtime rows reach the venue
Asterisk:

| | `shared_db` (default) | `agent` |
|---|---|---|
| Rows | the venue Asterisk reads DIAL's PostgreSQL over the VPN | a small **venue agent** next to Asterisk pulls snapshots over HTTPS into a local PostgreSQL; Asterisk reads that |
| Latency | none - a row is live once DIAL commits it | the poll interval (`agent_poll_interval`, default 15 s); DIAL nudges subscribers with the webhook `pbx.snapshot.changed` |
| Uplink down | provisioning frozen, registrations fail once cached rows expire | the venue keeps the last snapshot: local calls, forwarding, voicemail keep working |
| Venue → DIAL | PostgreSQL `5432` **and** HTTPS | HTTPS only |
| DIAL → venue | ARI/AMI for originate, reload, status | optional: the agent reloads Asterisk itself and reports registrations (`ps_contacts`) in its heartbeat; DIAL-initiated calls need ARI to be reachable regardless |
| Secret at the venue | the database password (every event) | this event's hook secret |

Switch it on `/e/<slug>/pbx/` → *PBX connection* → *Provisioning*, or scripted with
`dial pbx connection set --event <slug> --provisioning agent|shared_db --agent-poll-interval N`
(`PATCH /api/v1/pbx/connection/?event=`). In agent mode the ARI URL may stay empty - DIAL then leaves
reloads to the agent and treats "no ARI" as *no ARI*, not as "use the server default". Snapshots contain
exactly the rows the shared-database backend would write for this event: `ps_endpoints` by
`accountcode` = slug or `context` = `dial-<slug>`, `ps_auths`/`ps_aors`/`ps_endpoint_id_ips`/`ps_contacts`
via those endpoint ids, `extensions` and `voicemail_users` by context. The venue database schema comes
from `manage.py pbx_venue_schema [--out file]` (or `GET /api/v1/pbx/snapshot/schema/?event=`; the agent
applies it itself at startup). Why pull instead of push, and what the trade-off costs, is in the
architecture doc (sections 8 and 9).

### Running the venue agent

The venue agent replaces the shared database for boxes at the event site: it mirrors **one** event's
PBX configuration from DIAL into a local PostgreSQL that Asterisk reads via Realtime - over HTTPS only.
No database port has to be open between venue and DIAL, and the venue keeps working on the last
snapshot when the uplink drops. The agent itself (`deploy/venue-agent/dial_venue_agent.py`, stdlib +
`psycopg`), its environment, systemd unit and offline behaviour are documented in
[`deploy/venue-agent/README.md`](../deploy/venue-agent/README.md); the Asterisk side and the compose
stack in [`deploy/asterisk/README.md`](../deploy/asterisk/README.md) (*Deployment modes*). The short
version:

1. In DIAL open `/e/<slug>/pbx/` → *PBX connection*: set *Provisioning* to **Venue agent**, set or note
   the **hook secret**. The *Venue agent* card now shows the environment block to copy (`DIAL_URL`,
   `DIAL_EVENT`, `DIAL_PBX_HOOK_SECRET`) plus the snapshot, schema and heartbeat URLs.
2. On the venue box: `cd deploy/asterisk && cp .env.venue.example .env.venue && chmod 0600 .env.venue`;
   fill in `DIAL_URL` (https!), `DIAL_EVENT`, `DIAL_PBX_HOOK_SECRET`, the DB/ARI/AMI passwords and
   `DIAL_EMERGENCY_FALLBACK`. Instead of the hook secret a service token with scope `pbx:sync`
   (`DIAL_SYNC_TOKEN`) works too.
3. `docker compose -f docker-compose.venue.example.yml --env-file .env.venue up -d --build` - starts
   `venue-db`, `venue-agent` and the unchanged DIAL `asterisk` image pointed at the local database with
   `DIAL_CDR_HOOK=yes`.
4. Verify: `docker compose -f docker-compose.venue.example.yml --env-file .env.venue run --rm venue-agent --check`,
   then look at DIAL: the *Venue agent* card turns **online** with host, agent version and registered
   handsets (heartbeat with every poll, default every 15 s); `dial pbx agent status --event <slug>` shows
   the same. Also mirrored as one line on the orga dashboard's *Venue connection* card.
5. Changes in DIAL reach the venue within the poll interval (a heartbeat answered with *behind* makes the
   agent re-poll after 2 s); `logs -f venue-agent` prints one line per applied snapshot.
   `dial pbx snapshot --event <slug> [--out file]` dumps what the agent would receive.

**Offline.** Calls between local extensions, forwarding, voicemail, MWI and conferences keep working;
call groups, IVR menus, feature codes, DECT claim, record-by-phone, unknown/external numbers and
DIAL-initiated calls need the uplink. Emergency numbers fall back to `DIAL_EMERGENCY_FALLBACK` (section
15). CDRs are POSTed live (`DIAL_CDR_HOOK=yes`); calls made offline stay in the venue's local `cdr` table.

**Security.** The hook secret reveals the event's SIP passwords - keep `.env.venue` at mode `0600`, use
TLS, and **rotate the secret in DIAL** (PBX connection) if a box goes missing. One agent = one event =
one database; never point two agents at the same venue database.

**Status semantics.** *stale* = no heartbeat for three poll intervals (`agent_is_stale`); *behind* = the
agent's applied snapshot version differs from what DIAL would serve now (`agent_behind`). Both are in
`GET /api/v1/pbx/connection/?event=` as `agent_is_stale` / `agent_behind` / `snapshot_version`.

## 10. Running the helpdesk

- **Moderation queue** `/e/<slug>/orga/queue/` (`dial queue --event <slug>`): approve/reject with a note;
  the requester is notified, webhooks fire, provisioning starts.
- **Lookup** `/e/<slug>/orga/helpdesk/`: search by number, e-mail, nickname or IPEI; open the extension /
  device page, **reissue PIN** (`/devices/<id>/pin/`), rotate SIP password, check `provision_error`.
- **Number history**: when the search term is all digits the lookup page adds a card *History of
  <n>* - every extension that ever carried the number in this event (owner, state, devices, moderation
  and audit entries), newest first. Other events appear only if you are superuser or orga/admin/helpdesk
  there. Answers "who had 2323 before?" and "why does 4242 ring the wrong handset?". API:
  `GET /api/v1/extensions/history/?event=<slug>&number=<n>`.
- **Business card**: the extension page has a *Business card* card with a vCard (`/e/<slug>/phonebook/<n>.vcf`),
  a QR code (`/…/<n>/qr.png`) and a printable card (`/…/<n>/card/`) - visible while the extension is
  active and either in the phonebook or viewed by owner/orga/helpdesk. Handy for badges and door signs.
- **Trunk blocks in the queue**: every SIP trunk request needs approval; check the block range and that
  the requester really runs a PBX before approving (section 4).
- **Transfers**: users start them (`/extensions/<id>/transfer/`); the recipient accepts via link within
  the expiry. Orga can transfer via `POST /api/v1/extensions/<id>/transfer/`.
- **Waitlist**: taken numbers can be waitlisted (`/waitlist/`); when the number frees up the first
  requester is notified.
- **Guest extensions** `/e/<slug>/orga/guests/`: create short-lived numbers with claim tokens and print
  the QR codes on badges; they expire at `expires_at` (event end by default).
- **Claims** `/e/<slug>/numbering/claims/`: reserve a number for a named person and send them the invite
  link (section 4). Use this instead of registering on someone's behalf.
- **Forwarding**: users configure forwarding to another extension of the event (always / delayed / busy /
  unanswered) on the web or from the handset with the plan's feature codes (`*21<n>`, `*22<n>`,
  `*23<n>`, `*20` off). DIAL rejects self-forwarding, inactive targets and loops, and switches forwarding off
  automatically (audit-logged) when the target is deleted, expires or is rejected - so "my calls
  stopped forwarding" usually means the target number went away. The target's page lists
  *Forwarded from*.
- **Ringback tones**: a *Failed* tone shows the conversion error on the extension page (missing
  `ffmpeg`, unsupported codec, > 5 MB). Orga can clear or replace a user's tone.

## 11. Callbacks, wake-up, test ringback

`/e/<slug>/callback/all/` (orga) lists pending CCBS/CCNR requests, scheduled calls and ringbacks and lets
you fire or cancel them. Requests expire after `DIAL_CALLBACK_DEFAULT_TTL_MINUTES` (30). Test ringback
calls back after `DIAL_TEST_RINGBACK_DELAY_SECONDS` (10). Wake-up calls retry `max_retries` times every
`retry_interval_minutes`; users can snooze. All originates go through `get_pbx().originate()`.

## 12. Call groups

`/e/<slug>/callgroups/`: create a group extension (type `group`), pick a strategy (ring all, round robin,
longest idle), ring timeout and wrap-up time, add member extensions. Members log in/out via web,
API or `*71`/`*72` from their handset. Typical setup: `1000` orga hotline, `4300` medics, `6002` security.

- **Group admins**: the owner can appoint admins (`/<id>/admins/add/`) who manage members and settings
  without owning the number.
- **Invites**: managers invite an extension by number (`/<id>/invite/`); the owner receives an e-mail
  and accepts/declines under *My call groups → invitations* (`/mine/`, `/invites/`, `/invites/<token>/`).
  Open invites can be cancelled; members can leave with their own extension. Webhooks:
  `callgroup.invited`, `callgroup.invite_accepted`, `callgroup.invite_declined`.
- **Ring delays** (ring-all only): each member has an optional delay in seconds
  (`/<id>/members/<mid>/settings/`) for escalation tiers - tier 1 rings immediately, tier 2 after 10 s,
  etc. Delayed members are dialled as `Local/<delay 3 digits>*<number>@dial-group` legs; the route API
  returns `waves` and `dial_string_waves` alongside the plain `dial_string`.
- **Shortcode** (e.g. `SEC`, `MED`): prepended to the caller name on ringing handsets (`[SEC] Alice`),
  exposed as `callerid_prefix` in the route API.
- **Nesting**: a group can be a member of another group (flattened to endpoints, max 3 levels; cycles
  are rejected).

## 13. Voicemail & MWI

Each endpoint extension can get a mailbox (`/e/<slug>/voicemail/settings/<ext>/`): PIN, greeting, e-mail
delivery. Asterisk stores messages in the shared `voicemail` volume and notifies DIAL through
`vm_notify.sh`; DIAL imports the audio for web playback and updates MWI on DECT via the OMM. Dial the plan's
voicemail number (9999) to listen from a handset.

## 14. Monitoring & stats

`/e/<slug>/stats/`: calls per hour, answered ratio, busiest extensions, per-RFP erlang and heatmap, CSV
export. Privacy: `cdr_aggregate_only` (no per-number CDRs), `cdr_retention_days` (event override of
`DIAL_CDR_RETENTION_DAYS`), users' own export/delete under `/stats/mine/`. CDRs arrive via the
`ingest_cdrs` sweep of the Asterisk `cdr` table or the `cdr` hook.

## 15. Emergency procedures

> **Warning.** DIAL is not a certified emergency system. Always publish the venue's real emergency phone
> numbers and radio channels independently of the DECT network. Test the routes before the event opens.

- Targets: `/e/<slug>/emergency/` maps each plan emergency number (112, 110) to a destination extension
  (usually a medics/security call group) with a `fallback_number`. Every call creates an
  `EmergencyIncident` and a webhook `emergency.triggered`.
- Priority: orga can raise `Extension.priority` (`/emergency/priority/<ext>/`); Asterisk receives it as
  `DIAL_PRIORITY` for preemption logic.
- Broadcast: `/emergency/broadcast/` originates an announcement to all (or a group's) handsets via
  `get_pbx().broadcast()`. Use sparingly - it rings every phone.
- Venue agent mode (section 9): while the uplink to DIAL is down the dialplan cannot ask DIAL for the
  emergency target. Set `DIAL_EMERGENCY_FALLBACK` on **every** venue Asterisk - otherwise emergency
  numbers play "no service" offline.

## 16. Federation with another DIAL

Enable flag `federation`. Add a peer at `/e/<slug>/federation/peer/new/` with a `remote_prefix`
(digits your users dial first), `sip_host`, transport TLS and SRTP on, optional auth. The `pjsip` view
renders the trunk snippet for Asterisk; the other side does the same. The public directory
(`/api/v1/federation/directory/`) lists events with their prefixes.

## 17. PSTN breakout

Enable flag `breakout` and `Event.allow_breakout`. Configure a `Trunk` (SIP host, auth, `outbound_prefix`
default `0`, `caller_id_default`), `OutboundRule`s (regex on the dialled number, `per_call_max_minutes`,
priority), `BreakoutPermission`s (per extension or group, `daily_minutes_limit`) and caller-ID mappings.
`authorize()` is asked on every call via the route API; usage is tracked in `BreakoutUsage`
(`/api/v1/breakout/usage/`). Blocked destinations are simply rules with `allowed=false`.

## 18. Backup & archiving

- Event JSON export: `/e/<slug>/orga/export/` or `dial events export <slug>`.
- Database: `docker compose exec db pg_dump -U dial dial | gzip > dial-$(date +%F).sql.gz`.
- Volumes: `media` (logos, venue maps, greetings, voicemail audio, imported phone recordings),
  `voicemail` (Asterisk spool), `recordings` (raw phone recordings, shared with Asterisk),
  `asterisk-keys` (TLS certs).
- After archiving, consider running retention with `cdr_retention_days=1` to drop personal call data.

## 19. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| Handset won't subscribe | device page: PIN still valid? `omm_ppn` set? `provision_error`? `dial dect alerts --event <slug>` | reissue PIN, click provision, verify `OMM_*` env and that the OMM accepts the AXI user |
| SIP endpoint not registering | `asterisk -rx "pjsip show endpoints"`, `realtime load ps_endpoints id <user>`; password/transport on device page | resync event, check `ASTERISK_SIP_DOMAIN` = `SIP_DOMAIN`, firewall 5060/5061 |
| No audio / one-way audio | Docker NAT, `EXTERNAL_IP` empty, RTP range not published | set `EXTERNAL_IP` or `network_mode: host`, open 10000-10200/udp |
| Realtime rows missing | `select * from extensions where context='dial-<slug>'`; `PBXSyncLog` | `POST /api/v1/pbx/resync/`, check that `DIAL_PBX_BACKEND` is the Asterisk backend and worker logs |
| OMM unreachable alert | worker logs, `openssl s_client -connect OMM_HOST:12622` | network/firewall, `OMM_VERIFY_TLS=0` for self-signed certs, correct AXI credentials |
| Celery tasks not running | `docker compose logs worker beat`, `CELERY_TASK_ALWAYS_EAGER` | must be `0` in compose; Redis healthy; restart worker/beat |
| `429 Too many requests` | `DIAL_RATE_LIMITS` / DRF throttles, shared NAT at the venue | raise limits via settings, or exempt the helpdesk network at the proxy |
| CSRF errors behind proxy | `CSRF_TRUSTED_ORIGINS`, `DIAL_PUBLIC_URL`, `X-Forwarded-Proto` | add `https://<host>` to `CSRF_TRUSTED_ORIGINS`, set secure-cookie flags, forward proto header |
| Hooks return 403 | `X-DIAL-PBX-Secret` mismatch | align `DIAL_PBX_HOOK_SECRET` (DIAL) with the Asterisk container |
| Dialplan context missing at boot | DIAL not up when Asterisk started | set `DIAL_EVENTS=<slugs>` or `asterisk -rx "dialplan reload"` |
| Extension shows `PBX: <error>` / dead outbox jobs | `GET /api/v1/pbx/outbox/?event=`, `dial pbx outbox --event <slug>`, worker logs | fix PBX reachability (ARI/DB), then `dial pbx outbox --retry-dead` or admin *Retry*; check beat is running (`pbx-outbox-drain`) |
| Ringback tone stuck in *Processing* / *Failed* | worker logs, `ffmpeg -version` on the worker | install `ffmpeg` in the worker image or upload WAV; re-upload the file |
| Caller hears normal ring despite *Ready* tone | `asterisk -rx "moh show classes"` lacks `dial-<slug>-<number>` | regenerate `dial-moh.conf` from `render_musiconhold(event)`, `moh reload`, check Asterisk can read `MEDIA_ROOT/ringback/processed/` |
| Number "conflicts with 23" although 2323 is free | prefix-free numbering (`NumberPlan.prefix_free`) | intended; pick another number, or disable prefix-free on the plan |
| Registration mails not arriving | `EMAIL_URL`, rate limit (5/e-mail/h, 20/IP/h), token TTL | check the mail backend, wait for the window, `DIAL_EMAIL_TOKEN_TTL_HOURS` |
| Phone fetches `/prov/<vendor>/<mac>.cfg` and gets `401` | MAC route requires `?token=` or Basic `sip_username:sip_password` | configure Basic auth in the phone or use the per-device token URL |
| GSM SIM not linked | `POST /prov/gsm/register/` response, `X-DIAL-PBX-Secret`, token expired/used | reissue the code (`/devices/gsm/<id>/token/`), align the hook secret in the GSM core |
| Scheduled state change did not happen | `docker compose logs beat` for `events-apply-scheduled-transitions`; audit log of the event | beat must run; a schedule targeting a state already passed is dropped - set it again |
| Phone recording accepted, announcement still has no audio | `announcement-recorded` hook in web logs ("cannot read"), `DIAL_RECORDING_DIR` on web/worker vs. Asterisk | mount the `recordings` volume in web, worker **and** asterisk; same path on both sides or `DIAL_RECORD_DIR` |
| Softphone rejects the Linphone/Acrobits QR | `curl https://<dial>/prov/<token>/linphone.xml` | the XML formats are unverified on real apps - fall back to the generic `sip:` QR or *Manual settings* |
| Venue agent **stale** on `/e/<slug>/pbx/` or the orga dashboard | no heartbeat for 3× the poll interval: `dial pbx agent status --event <slug>`; on the box `docker compose … logs venue-agent` and `run --rm venue-agent --check` | uplink/TLS/`DIAL_URL`; hook secret must match the PBX connection (heartbeat answers `403` otherwise); agent container running. The venue keeps working on its last snapshot meanwhile |
| Venue agent **behind** for longer than a poll | agent *message* on the card (reload failed?), *Asterisk* badge, `agent_message` in the API | fix AMI credentials / `ASTERISK_RELOAD` on the box; `kill -HUP` the agent to force a full snapshot |
| Phone or OMM shows an empty / missing directory | `curl https://<dial>/e/<slug>/phonebook/remote/<token>/generic.xml`: `404` = token rotated, directory disabled, feature off or event not in *registration*/*live*; an empty list = nobody has *Public phonebook entry* ticked | copy the current URL from *Phonebook → Settings*, tick *Remote directory enabled*; Grandstream: path **without** `/phonebook.xml`; provisioned phones: `dial_provisioning_profiles --update` and re-provision |
| LDAP bind fails (`invalidCredentials`) or search returns nothing | bind DN `cn=directory,dc=<slug>,dc=dial`, password = the *current* token (rotated? cache is 30 s), event in *registration*/*live*, `ldap` service running and `3890` reachable | `ldapsearch -x -H ldap://<dial>:3890 -D cn=directory,dc=<slug>,dc=dial -w <token> -b ou=phonebook,dc=<slug>,dc=dial '(cn=*)'`; StartTLS is not supported - use LDAPS via `--cert` |
| SSO login: "the address is not verified yet" | an account with that e-mail exists but never confirmed it, and the provider did not assert `email_verified` | user logs in with the password, verifies the address (*Resend*) and tries SSO again - or links SSO from the profile page |
| SSO callback fails / "provider is misconfigured" | `manage.py dial_oidc_check`; `dial.oidc` logger; redirect URI at the provider must be exactly `https://<host>/accounts/oidc/callback/` | fix issuer / client id / secret; discovery is cached 1 h (flush the cache to hurry); repeated failures trip the per-IP login lockout |
