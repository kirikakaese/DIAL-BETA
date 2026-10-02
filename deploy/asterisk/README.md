# PET reference Asterisk container

A self-contained Asterisk 20 (Debian bookworm packages) that gets **all** of its endpoint,
dialplan, voicemail and CDR data from PET's PostgreSQL database via ODBC realtime, and talks back
to PET through the authenticated hook/route HTTP API. Nothing is downloaded at runtime: sounds,
modules and the ODBC driver are installed at image build time.

```
softphone/DECT ──SIP──▶ Asterisk ──ODBC realtime──▶ PostgreSQL ◀── Django (apps.pbx writes rows)
                           │                              ▲
                           ├──CURL() hooks / route API───▶ PET (apps/pbx/api.py)
                           └──ARI 8088 / AMI 5038 ◀────── PET (AsteriskPBX: originate/hangup/status)
```

## Files

| Path | Purpose |
|------|---------|
| `Dockerfile` | `debian:bookworm-slim` + `asterisk asterisk-modules asterisk-config asterisk-core-sounds-en-gsm asterisk-moh-opsound-gsm odbc-postgresql unixodbc curl gettext-base ...` |
| `entrypoint.sh` | renders `conf/*` with `envsubst` into `/etc/asterisk`, writes `odbc.ini`/`odbcinst.ini`, generates a self-signed TLS cert into `/etc/asterisk/keys` if none is mounted, waits for PostgreSQL, then `exec asterisk -f -U asterisk -G asterisk` |
| `conf/` | config **templates** (only the `${PET_*}`, `${DB_*}`, `${ARI_*}`, `${AMI_*}`, `${SIP_DOMAIN}`, `${RTP_*}` placeholders are substituted; Asterisk's own `${EXTEN}` etc. are untouched) |
| `scripts/pet_contexts.sh` | `#exec`'d by `extensions.conf`: fetches the `[pet-<slug>]` shell contexts from PET (`GET /api/v1/pbx/dialplan/?shell=1`), falls back to `PET_EVENTS` |
| `scripts/vm_notify.sh` | `externnotify` of app_voicemail → `POST /api/v1/pbx/hooks/voicemail/` |
| `scripts/ari_smoke.sh` | curl+jq smoke test of ARI (info, endpoints, channels) |
| `scripts/render_check.sh` | renders the templates locally and checks the substitution (no Docker needed) |
| `docker-compose.example.yml` | compose fragment to merge into the main stack (*shared database* mode) |
| `docker-compose.venue.example.yml`, `.env.venue.example` | complete stack for a venue box: local `venue-db` + `venue-agent` + this image (*venue agent* mode, see below) |

## Deployment modes: shared database vs. venue agent

| | Shared database (default) | Venue agent |
|---|---|---|
| Where | Asterisk runs next to PET (same host / VPN) | Asterisk at the venue, PET somewhere else |
| Realtime tables | PET's own PostgreSQL (`DB_HOST: db`) - `apps.pbx` writes rows directly, changes are live | a **local** PostgreSQL (`DB_HOST: venue-db`) fed by [`deploy/venue-agent`](../venue-agent/README.md): it polls `GET /api/v1/pbx/snapshot/` over HTTPS and replaces the tables in one transaction, then reloads Asterisk. Changes arrive within the poll interval (15 s) |
| Database access from the venue | required | **none** - only HTTPS to PET |
| CDRs | Asterisk writes `cdr`, PET's `ingest_cdrs` sweep reads it | Asterisk writes the *local* `cdr`; set **`PET_CDR_HOOK=yes`** so `[pet-hangup]` also POSTs every CDR to `/api/v1/pbx/hooks/cdr/` - the sweep never sees the venue database |
| Registrations (`ps_contacts`) | visible to PET directly | written locally by Asterisk, reported to PET with every agent heartbeat |
| Compose file | `docker-compose.example.yml` | `docker-compose.venue.example.yml` + `.env.venue` |

The venue database serves exactly **one event** (the agent wipes and rewrites every table on each change).
Set `PET_EVENTS=<slug>` on the venue Asterisk so the `[pet-<slug>]` shell context exists even when PET is
unreachable at boot (`scripts/pet_contexts.sh` falls back to it).

**What still needs PET at call time - in both modes.** Only the realtime *rows* live in the database;
hooks and the route API are HTTP calls from the dialplan to `PET_API_URL` (short `[pet-curl]` timeouts,
so a dead PET never hangs a call). When the venue loses its uplink:

- **Works** from the local rows: SIP registration/auth (`ps_endpoints`/`ps_auths`/`ps_aors`),
  extension-to-extension calls including forwarding, busy/no-answer branches and `VoiceMail()`
  (`voicemail_users` is local, MWI too), `ConfBridge` conferences, static `[pet-services]` such as
  echo / time / MOH, `app` extensions.
- **Fails or degrades** because `[pet-lookup]` / `[pet-hook]` cannot reach PET: call groups (`[pet-group]`
  gets no members → "nobody available"), IVR menus (`[pet-ivr]` plays the default greeting, options are
  unknown → invalid), unknown numbers / federation / breakout (`[pet-internal]` → `[pet-route]` → `pbx-invalid`),
  feature codes (`*66`, `*21`… → "invalid"), DECT claim, record-by-phone, wake-up / callback / site survey.
  Emergency numbers (`[pet-emergency]`) dial `PET_EMERGENCY_FALLBACK` when set, otherwise play
  "no service" - **set it on every venue box**.
- **Silently lost**: `extension-idle` and `cdr` hooks in `[pet-hangup]` and the voicemail `externnotify`
  (fire-and-forget `CURL()`). Offline CDRs stay in the local `cdr` table only.
- PET → Asterisk (ARI/AMI: originate, broadcast, wake-up calls, extension status) needs PET to reach the
  venue's port 8088 - in venue mode that means a VPN or port forwarding, or those features are
  unavailable regardless of the uplink. Likewise PET cannot read the venue's voicemail spool / recording
  volume (web playback of voicemails, import of phone-recorded announcements).

The agent side of the story (offline restarts, state directory, heartbeat) is in
[`deploy/venue-agent/README.md`](../venue-agent/README.md).

## Realtime tables ↔ Django models

All tables are *managed* Django models in `apps/pbx/models.py`; run `manage.py migrate` before
starting Asterisk. Column names are Asterisk option names (sorcery rejects unknown columns), booleans
are `yes`/`no` strings.

| Asterisk table (`extconfig.conf`) | Django model | Written by | Read by |
|---|---|---|---|
| `ps_endpoints` | `PsEndpoint` | `AsteriskPBX.sync_device` (one row per SIP `Device`; `context=pet-<slug>`, `set_var=PET_EVENT=<slug>`, `accountcode=<slug>`) | res_pjsip (sorcery `endpoint=realtime,ps_endpoints`) |
| `ps_auths` | `PsAuth` | `sync_device` (`username`/`password` = `Device.sip_username/sip_password`) | res_pjsip |
| `ps_aors` | `PsAor` | `sync_device` (`max_contacts` 1 for DECT/OMM, `mailboxes=<number>@pet-<slug>`) | res_pjsip |
| `ps_contacts` | `PsContact` | **Asterisk** (registrations) | res_pjsip, PET status/admin |
| `ps_endpoint_id_ips` | `PsEndpointIdIp` | admin (trunks, OMM by IP) | res_pjsip_endpoint_identifier_ip |
| `extensions` | `DialplanEntry` | `sync_extension` / `sync_event` (`apps.pbx.dialplan`) | pbx_realtime (`switch => Realtime/@` in every `[pet-<slug>]`) |
| `voicemail_users` | `VoicemailUser` | `sync_extension` (endpoint + voicemail extensions when the `voicemail` flag is on) | app_voicemail (`voicemail => odbc,asterisk,voicemail_users`) |
| `cdr` | `Cdr` | **Asterisk** (cdr_adaptive_odbc, `usegmtime=yes`) | `apps.pbx.tasks.ingest_cdrs` sweep → `apps.stats` |

`PBXSyncLog` (PET-only) records every provisioning push.

## Dialplan: what happens when someone dials `4242`

Static part: `conf/extensions.conf`. Per-event part: rows in `extensions`, context `pet-<slug>`.

1. The handset's endpoint row has `context=pet-demo`, so the INVITE lands in `[pet-demo]`. That
   context is a generated *shell* (from `scripts/pet_contexts.sh`):
   ```
   [pet-demo]
   include => pet-internal
   switch => Realtime/@
   ```
   Asterisk checks the realtime `extensions` table for `context=pet-demo, exten=4242` **before**
   the `pet-internal` include.
2. Rows written by `apps.pbx.dialplan.rows_for_extension` (parallel ring, two devices):
   ```
   4242,1  Set(__PET_EVENT=demo)
   4242,2  Set(PET_EXTEN=4242)
   4242,3  Set(PET_ALLOW_CB=1)
   4242,4  Set(PET_PRIORITY=0)
   4242,5  Set(CHANNEL(hangup_handler_push)=pet-hangup,s,1)
   4242,6  Set(CHANNEL(language)=en)      ; extension's announcement language, else event default
   4242,7  Dial(PJSIP/demo-aaaa&PJSIP/demo-bbbb,30,tT)
   4242,8  GotoIf($["${DIALSTATUS}" = "BUSY"]?11)
   4242,9  VoiceMail(4242@pet-demo,u)      ; or Goto(pet-demo,<forward_noanswer>,1) / Playback(vm-nobodyavail)
   4242,10 Hangup()
   4242,11 VoiceMail(4242@pet-demo,b)      ; or Goto(pet-demo,<forward_busy>,1) / Busy(10)
   4242,12 Hangup()
   ```
   The preamble always contains the `Set(CHANNEL(language)=…)` row (priority 6), so the `Dial` is at
   priority 7 and the busy/no-answer branches moved down by one compared to earlier releases - check
   any custom `Goto`s into `pet-<slug>` extens accordingly.
   Serial ring emits one `Dial` per device (with `Wait(<ring_delay>)`), `forward_unconditional`
   emits a single `Goto`. Per-extension features change individual rows:
   - **Forwarding** (`forward_mode` + `forward_target`): `always` → `Goto(pet-<slug>,<target>,1)`
     instead of the Dial; `delayed` → Dial with timeout `forward_delay`, then `Goto`; `busy` /
     `noanswer` → `Goto` in the respective branch. Legacy free-text `forward_*` fields apply only
     when the mode is `off`.
   - **Custom ringback tone**: a *ready* tone adds the Dial option `m(pet-<slug>-<number>)`, i.e. the
     caller hears that music-on-hold class instead of ringing (see *Music on hold* below).
   - **Call waiting off** (and every DECT endpoint): `ps_endpoints.device_state_busy_at=1`, so a second
     caller gets BUSY. **Caller-ID display** mode changes the endpoint's `callerid` column
     (`"4242 Alice" <4242>`, name only or number only). The extension's language is also written to
     `ps_endpoints.language`.
3. Other extension types jump into static contexts:
   `group` → `Gosub(pet-group,s,1(<n>))` (members from the route API),
   `ivr`/`announcement` → `Gosub(pet-ivr,s,1(<n>,<type>))` (greeting/options from the route API),
   `conference` → `ConfBridge(pet-<slug>-<n>,pet_bridge,pet_user)` (optional `Authenticate(pin)`),
   `voicemail` → `VoiceMail(<n>@pet-<slug>,u)`, `app` → `Gosub(pet-app,s,1(<app>))` → `[pet-services]`,
   `federation`/`breakout` → `Gosub(pet-route,s,1(<n>))`. GSM devices are dialled as
   `PJSIP/<msisdn>@<Event.gsm_trunk>` (default `gsm-gateway`, see *GSM gateway trunk* below).
4. Number-plan rows (`rows_for_plan`): service numbers `Goto(pet-services,<echo|ringback-request|wakeup|survey|voicemail|dect-claim|record-announcement>,1)`
   (the last two additionally get a `_<number>X.` pattern so `<number><code>` can be dialled en bloc,
   passing the code as `PET_CLAIM_CODE` / `PET_RECORD_CODE`),
   feature codes `*66` / `_*66.` → `Gosub(pet-feature,s,1(*66,${EXTEN:3}))` (likewise `*86`, `*71`/`*72`,
   `*21`/`*22`/`*23`/`*20` for forwarding), emergency numbers →
   `Goto(pet-emergency,<n>,1)`.
5. Numbers with no realtime row fall through to `[pet-internal]` → `Gosub(pet-route,...)`, which asks
   the route API (emergency / federation / breakout) and dials whatever PET returns.
6. On hangup `[pet-hangup]` POSTs `extension-idle` for caller and callee (fires pending CCBS) and,
   when `PET_CDR_HOOK=yes`, the `cdr` hook.

`[pet-services]` also contains the *originate targets* used by `AsteriskPBX.originate()`
(`POST /ari/channels endpoint=Local/<dest>@pet-<slug> context=pet-services extension=<PET_SERVICE>`):
`announce` (default; `PET_ANNOUNCEMENT`, used by `broadcast()`), `callback` (`PET_CALLBACK_TARGET`),
`ringback`, `wakeup-call`, plus `echo`, `time`, `moh`, `hello` for `app` extensions.

### Call groups with ring delays (`[pet-group]`)

`[pet-group]` asks the route API for the group and dials `dial_string` (or, when present,
`dial_string_waves`) with `callerid_prefix` prepended to the caller name (`[SEC] Alice`). Members
with a ring delay in ring-all mode are not dialled directly but as **delayed legs**:

```
Local/<delay, 3 digits>*<number>@pet-group        e.g. Local/010*4300@pet-group
```

The static exten `_XXX*X.` in `[pet-group]` does `Wait(<delay>)` and then
`Dial(Local/<number>@pet-${PET_EVENT},...)`, so the member rings through its normal route in the event
context after the delay. The outer `Dial()` tears these legs down when someone else answers or the
group timeout expires. Nested groups are flattened by PET before they reach the dialplan, so Asterisk
only ever sees endpoint numbers. Older PET versions simply don't send `dial_string_waves` /
`callerid_prefix`; the context ignores absent keys.

### Music on hold for custom ringback tones (`pet-moh.conf`)

`conf/musiconhold.conf` ends with `#include pet-moh.conf`. That file is **not** realtime: PET renders one

```
[pet-<slug>-<number>]
mode=files
directory=<MEDIA_ROOT>/ringback/processed/<extension id>/
```

class per *ready* tone via `AsteriskPBX.render_musiconhold(event)`. Write the output to
`/etc/asterisk/pet-moh.conf` and run `asterisk -rx "moh reload"` whenever tones change; keep at least
an empty file there so the include does not log warnings. The processed files are 8 kHz mono WAV produced by
the PET worker (`ffmpeg` needed for non-WAV uploads), so the Asterisk container needs read access to
`MEDIA_ROOT/ringback/processed/` - mount PET's `media` volume. The generated `Dial` uses `m(pet-<slug>-<number>)`
to play the class to the caller.

### Trunk extensions (number blocks)

An extension of type `trunk` hands a whole block of numbers (`4700`–`4799`; block sizes 10/100/1000, base
ends in as many zeros) to a **remote PBX** that registers once with the trunk's single SIP account.
`rows_for_trunk` writes two extens: the base number and an Asterisk pattern for the block. Both set
`PET_TRUNK=<base>` and dial the number *unchanged* as request-URI user of the bound endpoint:

```
4700,1   Set(__PET_EVENT=demo)
4700,2   Set(PET_EXTEN=4700)
4700,3   Set(PET_ALLOW_CB=1)
4700,4   Set(PET_PRIORITY=0)
4700,5   Set(CHANNEL(hangup_handler_push)=pet-hangup,s,1)
4700,6   Set(CHANNEL(language)=en)
4700,7   Set(PET_TRUNK=4700)
4700,8   Dial(PJSIP/4700@demo-pbx,30,tT)
4700,9   GotoIf($["${DIALSTATUS}" = "BUSY"]?12)
4700,10  Playback(vm-nobodyavail)
4700,11  Hangup()
4700,12  Busy(10)
4700,13  Hangup()
_47XX,1  Set(__PET_EVENT=demo)
_47XX,2  Set(PET_EXTEN=${EXTEN})
...      (same preamble)
_47XX,7  Set(PET_TRUNK=4700)
_47XX,8  Dial(PJSIP/${EXTEN}@demo-pbx,30,tT)
...      (same busy / no-answer rows)
```

Asterisk prefers the exact match, so `4700` wins over `_47XX`; a number of the block that is registered as
a normal extension cannot exist (PET refuses both directions of that overlap). Without a bound SIP account
the rows play `vm-nobodyavail`. Trunks have no mailbox. `remove_extension` / `sync_event` prune the pattern
row together with the base (`dialplan.extens_for_extension`).

The route API answers for every number of an *active* block, so `[pet-internal]` → `[pet-lookup]` works
even without the realtime pattern rows:

```
GET /api/v1/pbx/route/?event=demo&number=4711
{"type": "trunk", "targets": ["PJSIP/4711@demo-pbx"], "dial_string": "PJSIP/4711@demo-pbx",
 "timeout": 30, "priority": 0, "trunk": {"base": "4700", "range": ["4700", "4799"]}, ...}
```

**Caller-ID limitation.** The trunk's SIP account is an ordinary `ps_endpoints` row: `callerid` is the
*block base* (`"Village PBX" <4700>`) and `trust_id_inbound=no`, so every outgoing call from the remote
PBX is presented as `4700`, whatever From/P-Asserted-Identity it sends. Per-number caller-ID passthrough
(`4711` calling out as `4711`) is **not implemented**; it would need `trust_id_inbound=yes` plus a dialplan
check that the asserted number lies inside `PET_TRUNK`'s block.

### GSM gateway trunk (`pjsip.conf`)

`conf/pjsip.conf` contains a **commented** `[gsm-gateway]` template (endpoint + aor + identify) for an
on-site cell network (e.g. `osmo-sip-connector` in front of `osmo-msc`). Uncomment it, set the gateway IP
in `contact=` / `match=`, and make sure the section name equals `Event.gsm_trunk` (default
`gsm-gateway`) for events with *has GSM* enabled. PET dials GSM devices as `PJSIP/<msisdn>@<gsm_trunk>`.
The GSM core links SIMs to PET devices by calling `POST /prov/gsm/register/` (header
`X-PET-PBX-Secret`, body `{event, token, imsi, msisdn?}`) - the same secret as the hooks below.

## Hooks and route API (Asterisk → PET)

All requests carry `X-PET-PBX-Secret: ${PET_PBX_HOOK_SECRET}`. PET checks it against the **event's**
hook secret (orga page `/e/<slug>/pbx/`, *PBX connection*) when one is saved, otherwise against the
server-wide `settings.PET_PBX_HOOK_SECRET` (falling back to `ASTERISK_ARI_PASSWORD`). A venue box
therefore serves exactly the events whose secret it knows. Form-encoded POST bodies via `CURL()`; responses are JSON
and parsed with `JSON_DECODE`. Timeouts are short (`[pet-curl]`) so a dead PET never blocks a call.

| Call | Where | Body / query | Uses response |
|---|---|---|---|
| `POST /api/v1/pbx/hooks/feature-code/` | `[pet-feature]`, `ringback-request` (`code=ringback`), `wakeup` (`code=wakeup`, `target=HHMM`) | `event, caller, code, target` | `handled` → beep+thank you or `pbx-invalid`. Handlers: callback (`*66`/`*86`), call groups (`*71`/`*72`), call forwarding (`*21<n>` always, `*22<n>` busy, `*23<n>` no answer, `*20` off - `apps.extensions.feature_codes`) |
| `POST /api/v1/pbx/hooks/dect-claim/` | `dect-claim` service | `event, caller` (PJSIP endpoint), `callerid, code` | `handled, number` (announced with `SayDigits`) |
| `POST /api/v1/pbx/hooks/announcement-record-start/` | `record-announcement` service | `event, caller` (PJSIP endpoint), `callerid, code` | `handled, number, name, file` (`file` = absolute path without extension to `Record()` into; `name` = its basename, used with `PET_RECORD_DIR` when set) |
| `POST /api/v1/pbx/hooks/announcement-recorded/` | `record-announcement` service / `[pet-record-hangup]` | `event, code, file` (`<file>.wav`), `duration` (s) | `handled, number` → thank you or `pbx-invalid`; PET imports the wav into `MEDIA_ROOT/ivr/<slug>/<number>/` when it can read it, else keeps the path as playback reference |
| `POST /api/v1/pbx/hooks/extension-idle/` | `[pet-hangup]` | `event, number, channel` | – |
| `POST /api/v1/pbx/hooks/cdr/` | `[pet-hangup]` if `PET_CDR_HOOK=yes` | `event, src, dst, start, answer, end, duration, billsec, disposition, channel, dstchannel, uniqueid, dcontext, accountcode` | – |
| `POST /api/v1/pbx/hooks/voicemail/` | `scripts/vm_notify.sh` (externnotify) | `event, mailbox, caller, file_path, duration, new_messages, old_messages` | – |
| `POST /api/v1/pbx/hooks/site-survey/` | `survey` service loop | `event, caller` | `say` (RFP name, spelled with `SayAlpha`) |
| `GET /api/v1/pbx/route/?event=&number=` | `[pet-lookup]` (groups, IVR, unknown numbers, emergency, trunk blocks, federation/breakout) | – | `type, dial_string, strategy, timeout, forward_busy, forward_noanswer, forward_unconditional, ivr_greeting, ivr_timeout, ivr_options`; groups additionally `waves, dial_string_waves, callerid_prefix`; trunk blocks additionally `trunk: {base, range}` |
| `GET /api/v1/pbx/dialplan/?shell=1` | `scripts/pet_contexts.sh` at load / `dialplan reload` | – | text: `[pet-<slug>]` shell contexts |

PET → Asterisk: ARI (`/ari/asterisk/info`, `/channels`, `/endpoints/PJSIP/..`, `/deviceStates/..`,
`/mailboxes/..`) and, as fallback for originate and for `dialplan reload` after `sync_event`, AMI.

## Environment variables (docker-compose)

| Variable | Default | Must match |
|---|---|---|
| `PET_API_URL` | `http://pet:8000` | PET web service URL reachable from the container (a venue box uses the public `https://` URL over the VPN) |
| `PET_PBX_HOOK_SECRET` | `pet` | the event's hook secret from `/e/<slug>/pbx/`, or PET's server-wide `PET_PBX_HOOK_SECRET` (or `ASTERISK_ARI_PASSWORD`) when the event has none |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `db` / `5432` / `pet` / `pet` / `pet` | PET's `DATABASE_URL` (same database!) in shared-database mode, the local `venue-db` in venue-agent mode. `DATABASE_*` accepted as aliases |
| `ARI_USER` / `ARI_PASSWORD` | `pet` / `pet` | PET `ASTERISK_ARI_USER` / `ASTERISK_ARI_PASSWORD` |
| `AMI_USER` / `AMI_PASSWORD` | `pet` / `pet` | PET `ASTERISK_AMI_USER` / `ASTERISK_AMI_PASSWORD` |
| `SIP_DOMAIN` | `pet.local` | PET `ASTERISK_SIP_DOMAIN` (digest realm, TLS cert CN) |
| `EXTERNAL_IP` | – | host LAN IP when using bridge networking (sets `external_*_address`); alias `SIP_EXTERNAL_IP` |
| `PET_EVENTS` | – | optional comma-separated slugs used for shell contexts when PET is unreachable at boot |
| `PET_EMERGENCY_FALLBACK` | – | optional number dialled when PET has no emergency route |
| `PET_CDR_HOOK` | `no` | `yes` to also POST CDRs to the hook (otherwise use the `ingest_cdrs` sweep). Required in venue-agent mode |
| `PET_RECORD_DIR` | – | optional override of the directory `record-announcement` writes to; by default the path from the `announcement-record-start` response (PET's `PET_RECORDING_DIR`, `/var/spool/asterisk/pet-recordings`) is used. Mount the same volume in PET (`recordings` in docker-compose) so the wav can be imported |
| `RTP_START` / `RTP_END` | `10000` / `10200` | published UDP port range |

PET side: `PET_PBX_BACKEND=apps.pbx.backends.asterisk.AsteriskPBX`, `ASTERISK_ARI_URL=http://asterisk:8088/ari`,
`ASTERISK_AMI_HOST=asterisk`.

Ports: 5060/udp+tcp (SIP), 5061/tcp (SIP TLS, self-signed), 8088/tcp (HTTP/ARI - keep internal),
8089/tcp (WSS scaffold), 5038/tcp (AMI - keep internal), 10000-10200/udp (RTP). On the event LAN
prefer `network_mode: host` - SIP/RTP through Docker NAT is painful.

Volumes: `/var/spool/asterisk/voicemail` (share with PET for `apps.voicemail`),
`/var/spool/asterisk/pet-recordings` (announcements recorded by phone - share with PET so `apps.ivr` can import them),
`/var/lib/asterisk/sounds/pet` (custom prompts `pet/<name>`; core sounds are used until recorded),
`/etc/asterisk/keys` (mount `asterisk.crt`, `asterisk.key`, `asterisk.pem` to replace the self-signed cert),
PET's `media` volume read-only at the path `render_musiconhold()` emits (custom ringback tones), and
`/etc/asterisk/pet-moh.conf` (rendered MOH classes, see above).

## Testing with a softphone

1. `manage.py migrate`, start the stack, check `docker compose logs asterisk` for
   `[pet] starting` and no ODBC errors; `docker compose exec asterisk asterisk -rx "odbc show"`.
2. In PET create an event (`demo`), approve an extension `4242` and add a **SIP** device to it
   (or `POST /api/v1/pbx/resync/?event=demo` as orga). Check the rows:
   `asterisk -rx "pjsip show endpoints"`, `asterisk -rx "dialplan show 4242@pet-demo"`,
   `asterisk -rx "realtime load ps_endpoints id <sip_username>"`.
3. Register a softphone (Linphone, Zoiper, Baresip …): server = container/host IP, user/password =
   `Device.sip_username` / `sip_password` (visible on the device page), transport UDP/TCP 5060 or
   TLS 5061 (accept the self-signed cert). `asterisk -rx "pjsip show contacts"` lists it.
4. Dial the plan's echo test number (`9003` in the demo plan) → echo; `9001` wake-up prompt;
   `*66<number>` → feature-code hook (needs `apps.callback`).
5. Dial a second extension for a real call; hang up and watch `pet.pbx.api` logs for the
   `extension-idle` hook; `select * from cdr order by id desc limit 1;` shows the CDR.
6. From PET: `get_pbx().health()`, `extension_status(ext)`, `originate(event=..., destination="4242",
   caller_id="PET", variables={"PET_SERVICE": "announce", "PET_ANNOUNCEMENT": "hello-world"})`.
7. `scripts/ari_smoke.sh` inside the container or `PET_INTEGRATION=1 pytest tests/integration` from
   the host (see `tests/integration/README.md`).

## Notes / limitations

- `PET_PBX_HOOK_SECRET` and passwords are pasted into config files: avoid `;`, `$`, `{`, `}` and
  newlines in them.
- Timestamps in `cdr` are written as UTC (`usegmtime=yes`) into `timestamptz` columns: run the
  database with `TimeZone=UTC` (the default in the official postgres image).
- WebRTC (`transport-wss`, 8089) is a scaffold: the endpoint options are set by `sync_device` for
  `webrtc` devices, but PET has no browser softphone yet.
- MWI: app_voicemail drives MWI from `voicemail_users`/spool directly; `res_ari_mailboxes` is
  `noload`ed so `AsteriskPBX.set_mwi` logs a warning and returns (by design).
- Provisioning pushes from PET arrive through the **PBX outbox** (`apps/pbx/outbox.py`): if Asterisk or
  the database is down, rows are written later with backoff instead of being lost; `dialplan reload`
  after `sync_event` still goes via AMI. Watch `GET /api/v1/pbx/outbox/?event=` when "nothing
  changes" on the PBX.
- `pet-moh.conf` is static: a tone that is *ready* in PET but has no class in Asterisk plays the
  normal ring. Re-render and `moh reload`.
