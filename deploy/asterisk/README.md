# DIAL reference Asterisk container

A self-contained Asterisk 20 (Ubuntu 24.04 packages) that gets **all** of its endpoint,
dialplan, voicemail and CDR data from DIAL's PostgreSQL database via ODBC realtime, and talks back
to DIAL through the authenticated hook/route HTTP API. Nothing is downloaded at runtime: sounds,
modules and the ODBC driver are installed at image build time.

```
softphone/DECT ──SIP──▶ Asterisk ──ODBC realtime──▶ PostgreSQL ◀── Django (apps.pbx writes rows)
                           │                              ▲
                           ├──CURL() hooks / route API───▶ DIAL (apps/pbx/api.py)
                           └──ARI 8088 / AMI 5038 ◀────── DIAL (AsteriskPBX: originate/hangup/status)
```

## Files

| Path | Purpose |
|------|---------|
| `Dockerfile` | `ubuntu:24.04` + `asterisk asterisk-modules asterisk-config asterisk-core-sounds-en-gsm asterisk-moh-opsound-gsm odbc-postgresql unixodbc curl gettext-base ...` |
| `entrypoint.sh` | renders `conf/*` with `envsubst` into `/etc/asterisk`, writes `odbc.ini`/`odbcinst.ini`, generates a self-signed TLS cert into `/etc/asterisk/keys` if none is mounted, waits for PostgreSQL, then `exec asterisk -f -U asterisk -G asterisk` |
| `conf/` | config **templates** (only the `${DIAL_*}`, `${DB_*}`, `${ARI_*}`, `${AMI_*}`, `${SIP_DOMAIN}`, `${RTP_*}` placeholders are substituted; Asterisk's own `${EXTEN}` etc. are untouched) |
| `scripts/dial_contexts.sh` | `#exec`'d by `extensions.conf`: fetches the `[dial-<slug>]` shell contexts from DIAL (`GET /api/v1/pbx/dialplan/?shell=1`), falls back to `DIAL_EVENTS` |
| `scripts/vm_notify.sh` | `externnotify` of app_voicemail → `POST /api/v1/pbx/hooks/voicemail/` |
| `scripts/ari_smoke.sh` | curl+jq smoke test of ARI (info, endpoints, channels) |
| `scripts/render_check.sh` | renders the templates locally and checks the substitution (no Docker needed) |
| `docker-compose.example.yml` | compose fragment to merge into the main stack (*shared database* mode) |
| `docker-compose.venue.example.yml`, `.env.venue.example` | complete stack for a venue box: local `venue-db` + `venue-agent` + this image (*venue agent* mode, see below) |

## Deployment modes: shared database vs. venue agent

| | Shared database (default) | Venue agent |
|---|---|---|
| Where | Asterisk runs next to DIAL (same host / VPN) | Asterisk at the venue, DIAL somewhere else |
| Realtime tables | DIAL's own PostgreSQL (`DB_HOST: db`) - `apps.pbx` writes rows directly, changes are live | a **local** PostgreSQL (`DB_HOST: venue-db`) fed by [`deploy/venue-agent`](../venue-agent/README.md): it polls `GET /api/v1/pbx/snapshot/` over HTTPS and replaces the tables in one transaction, then reloads Asterisk. Changes arrive within the poll interval (15 s) |
| Database access from the venue | required | **none** - only HTTPS to DIAL |
| CDRs | Asterisk writes `cdr`, DIAL's `ingest_cdrs` sweep reads it | Asterisk writes the *local* `cdr`; set **`DIAL_CDR_HOOK=yes`** so `[dial-hangup]` also POSTs every CDR to `/api/v1/pbx/hooks/cdr/` - the sweep never sees the venue database |
| Registrations (`ps_contacts`) | visible to DIAL directly | written locally by Asterisk, reported to DIAL with every agent heartbeat |
| Compose file | `docker-compose.example.yml` | `docker-compose.venue.example.yml` + `.env.venue` |

The venue database serves exactly **one event** (the agent wipes and rewrites every table on each change).
Set `DIAL_EVENTS=<slug>` on the venue Asterisk so the `[dial-<slug>]` shell context exists even when DIAL is
unreachable at boot (`scripts/dial_contexts.sh` falls back to it).

**What still needs DIAL at call time - in both modes.** Only the realtime *rows* live in the database;
hooks and the route API are HTTP calls from the dialplan to `DIAL_API_URL` (short `[dial-curl]` timeouts,
so a dead DIAL never hangs a call). When the venue loses its uplink:

- **Works** from the local rows: SIP registration/auth (`ps_endpoints`/`ps_auths`/`ps_aors`),
  extension-to-extension calls including forwarding, busy/no-answer branches and `VoiceMail()`
  (`voicemail_users` is local, MWI too), `ConfBridge` conferences, static `[dial-services]` such as
  echo / time / MOH, `app` extensions.
- **Fails or degrades** because `[dial-lookup]` / `[dial-hook]` cannot reach DIAL: call groups (`[dial-group]`
  gets no members → "nobody available"), IVR menus (`[dial-ivr]` plays the default greeting, options are
  unknown → invalid), unknown numbers / federation / breakout (`[dial-internal]` → `[dial-route]` → `pbx-invalid`),
  feature codes (`*66`, `*21`… → "invalid"), DECT claim, record-by-phone, wake-up / callback / site survey.
  Emergency numbers (`[dial-emergency]`) dial `DIAL_EMERGENCY_FALLBACK` when set, otherwise play
  "no service" - **set it on every venue box**.
- **Silently lost**: `extension-idle` and `cdr` hooks in `[dial-hangup]` and the voicemail `externnotify`
  (fire-and-forget `CURL()`). Offline CDRs stay in the local `cdr` table only.
- DIAL → Asterisk (ARI/AMI: originate, broadcast, wake-up calls, extension status) needs DIAL to reach the
  venue's port 8088 - in venue mode that means a VPN or port forwarding, or those features are
  unavailable regardless of the uplink. Likewise DIAL cannot read the venue's voicemail spool / recording
  volume (web playback of voicemails, import of phone-recorded announcements).

The agent side of the story (offline restarts, state directory, heartbeat) is in
[`deploy/venue-agent/README.md`](../venue-agent/README.md).

## Realtime tables ↔ Django models

All tables are *managed* Django models in `apps/pbx/models.py`; run `manage.py migrate` before
starting Asterisk. Column names are Asterisk option names (sorcery rejects unknown columns), booleans
are `yes`/`no` strings.

| Asterisk table (`extconfig.conf`) | Django model | Written by | Read by |
|---|---|---|---|
| `ps_endpoints` | `PsEndpoint` | `AsteriskPBX.sync_device` (one row per SIP `Device`; `context=dial-<slug>`, `set_var=DIAL_EVENT=<slug>`, `accountcode=<slug>`) | res_pjsip (sorcery `endpoint=realtime,ps_endpoints`) |
| `ps_auths` | `PsAuth` | `sync_device` (`username`/`password` = `Device.sip_username/sip_password`) | res_pjsip |
| `ps_aors` | `PsAor` | `sync_device` (`max_contacts` 1 for DECT/OMM, `mailboxes=<number>@dial-<slug>`) | res_pjsip |
| `ps_contacts` | `PsContact` | **Asterisk** (registrations) | res_pjsip, DIAL status/admin |
| `ps_endpoint_id_ips` | `PsEndpointIdIp` | admin (trunks, OMM by IP) | res_pjsip_endpoint_identifier_ip |
| `extensions` | `DialplanEntry` | `sync_extension` / `sync_event` (`apps.pbx.dialplan`) | pbx_realtime (`switch => Realtime/@` in every `[dial-<slug>]`) |
| `voicemail_users` | `VoicemailUser` | `sync_extension` (endpoint + voicemail extensions when the `voicemail` flag is on) | app_voicemail (`voicemail => odbc,asterisk,voicemail_users`) |
| `cdr` | `Cdr` | **Asterisk** (cdr_adaptive_odbc, `usegmtime=yes`) | `apps.pbx.tasks.ingest_cdrs` sweep → `apps.stats` |

`PBXSyncLog` (DIAL-only) records every provisioning push.

## Dialplan: what happens when someone dials `4242`

Static part: `conf/extensions.conf`. Per-event part: rows in `extensions`, context `dial-<slug>`.

1. The handset's endpoint row has `context=dial-demo`, so the INVITE lands in `[dial-demo]`. That
   context is a generated *shell* (from `scripts/dial_contexts.sh`):
   ```
   [dial-demo]
   include => dial-internal
   switch => Realtime/@
   ```
   Asterisk checks the realtime `extensions` table for `context=dial-demo, exten=4242` **before**
   the `dial-internal` include.
2. Rows written by `apps.pbx.dialplan.rows_for_extension` (parallel ring, two devices):
   ```
   4242,1  Set(__DIAL_EVENT=demo)
   4242,2  Set(DIAL_EXTEN=4242)
   4242,3  Set(DIAL_ALLOW_CB=1)
   4242,4  Set(DIAL_PRIORITY=0)
   4242,5  Set(CHANNEL(hangup_handler_push)=dial-hangup,s,1)
   4242,6  Set(CHANNEL(language)=en)      ; extension's announcement language, else event default
   4242,7  Dial(PJSIP/demo-aaaa&PJSIP/demo-bbbb,30,tT)
   4242,8  GotoIf($["${DIALSTATUS}" = "BUSY"]?11)
   4242,9  VoiceMail(4242@dial-demo,u)      ; or Goto(dial-demo,<forward_noanswer>,1) / Playback(vm-nobodyavail)
   4242,10 Hangup()
   4242,11 VoiceMail(4242@dial-demo,b)      ; or Goto(dial-demo,<forward_busy>,1) / Busy(10)
   4242,12 Hangup()
   ```
   The preamble always contains the `Set(CHANNEL(language)=…)` row (priority 6), so the `Dial` is at
   priority 7 and the busy/no-answer branches moved down by one compared to earlier releases - check
   any custom `Goto`s into `dial-<slug>` extens accordingly.
   Serial ring emits one `Dial` per device (with `Wait(<ring_delay>)`), `forward_unconditional`
   emits a single `Goto`. Per-extension features change individual rows:
   - **Forwarding** (`forward_mode` + `forward_target`): `always` → `Goto(dial-<slug>,<target>,1)`
     instead of the Dial; `delayed` → Dial with timeout `forward_delay`, then `Goto`; `busy` /
     `noanswer` → `Goto` in the respective branch. Legacy free-text `forward_*` fields apply only
     when the mode is `off`.
   - **Custom ringback tone**: a *ready* tone adds the Dial option `m(dial-<slug>-<number>)`, i.e. the
     caller hears that music-on-hold class instead of ringing (see *Music on hold* below).
   - **Call waiting off** (and every DECT endpoint): `ps_endpoints.device_state_busy_at=1`, so a second
     caller gets BUSY. **Caller-ID display** mode changes the endpoint's `callerid` column
     (`"4242 Alice" <4242>`, name only or number only). The extension's language is also written to
     `ps_endpoints.language`.
3. Other extension types jump into static contexts:
   `group` → `Gosub(dial-group,s,1(<n>))` (members from the route API),
   `ivr`/`announcement` → `Gosub(dial-ivr,s,1(<n>,<type>))` (greeting/options from the route API),
   `conference` → `ConfBridge(dial-<slug>-<n>,dial_bridge,dial_user)` (optional `Authenticate(pin)`),
   `voicemail` → `VoiceMail(<n>@dial-<slug>,u)`, `app` → `Gosub(dial-app,s,1(<app>))` → `[dial-services]`,
   `federation`/`breakout` → `Gosub(dial-route,s,1(<n>))`. GSM devices are dialled as
   `PJSIP/<msisdn>@<Event.gsm_trunk>` (default `gsm-gateway`, see *GSM gateway trunk* below).
4. Number-plan rows (`rows_for_plan`): service numbers `Goto(dial-services,<echo|ringback-request|wakeup|survey|voicemail|dect-claim|record-announcement>,1)`
   (the last two additionally get a `_<number>X.` pattern so `<number><code>` can be dialled en bloc,
   passing the code as `DIAL_CLAIM_CODE` / `DIAL_RECORD_CODE`),
   feature codes `*66` / `_*66.` → `Gosub(dial-feature,s,1(*66,${EXTEN:3}))` (likewise `*86`, `*71`/`*72`,
   `*21`/`*22`/`*23`/`*20` for forwarding), emergency numbers →
   `Goto(dial-emergency,<n>,1)`.
5. Numbers with no realtime row fall through to `[dial-internal]` → `Gosub(dial-route,...)`, which asks
   the route API (emergency / federation / breakout) and dials whatever DIAL returns.
6. On hangup `[dial-hangup]` POSTs `extension-idle` for caller and callee (fires pending CCBS) and,
   when `DIAL_CDR_HOOK=yes`, the `cdr` hook.

`[dial-services]` also contains the *originate targets* used by `AsteriskPBX.originate()`
(`POST /ari/channels endpoint=Local/<dest>@dial-<slug> context=dial-services extension=<DIAL_SERVICE>`):
`announce` (default; `DIAL_ANNOUNCEMENT`, used by `broadcast()`), `callback` (`DIAL_CALLBACK_TARGET`),
`ringback`, `wakeup-call`, plus `echo`, `time`, `moh`, `hello` for `app` extensions.

### Call groups with ring delays (`[dial-group]`)

`[dial-group]` asks the route API for the group and dials `dial_string` (or, when present,
`dial_string_waves`) with `callerid_prefix` prepended to the caller name (`[SEC] Alice`). Members
with a ring delay in ring-all mode are not dialled directly but as **delayed legs**:

```
Local/<delay, 3 digits>*<number>@dial-group        e.g. Local/010*4300@dial-group
```

The static exten `_XXX*X.` in `[dial-group]` does `Wait(<delay>)` and then
`Dial(Local/<number>@dial-${DIAL_EVENT},...)`, so the member rings through its normal route in the event
context after the delay. The outer `Dial()` tears these legs down when someone else answers or the
group timeout expires. Nested groups are flattened by DIAL before they reach the dialplan, so Asterisk
only ever sees endpoint numbers. Older DIAL versions simply don't send `dial_string_waves` /
`callerid_prefix`; the context ignores absent keys.

### Music on hold for custom ringback tones (`dial-moh.conf`)

`conf/musiconhold.conf` ends with `#include dial-moh.conf`. That file is **not** realtime: DIAL renders one

```
[dial-<slug>-<number>]
mode=files
directory=<MEDIA_ROOT>/ringback/processed/<extension id>/
```

class per *ready* tone via `AsteriskPBX.render_musiconhold(event)`. Write the output to
`/etc/asterisk/dial-moh.conf` and run `asterisk -rx "moh reload"` whenever tones change; keep at least
an empty file there so the include does not log warnings. The processed files are 8 kHz mono WAV produced by
the DIAL worker (`ffmpeg` needed for non-WAV uploads), so the Asterisk container needs read access to
`MEDIA_ROOT/ringback/processed/` - mount DIAL's `media` volume. The generated `Dial` uses `m(dial-<slug>-<number>)`
to play the class to the caller.

### Trunk extensions (number blocks)

An extension of type `trunk` hands a whole block of numbers (`4700`–`4799`; block sizes 10/100/1000, base
ends in as many zeros) to a **remote PBX** that registers once with the trunk's single SIP account.
`rows_for_trunk` writes two extens: the base number and an Asterisk pattern for the block. Both set
`DIAL_TRUNK=<base>` and dial the number *unchanged* as request-URI user of the bound endpoint:

```
4700,1   Set(__DIAL_EVENT=demo)
4700,2   Set(DIAL_EXTEN=4700)
4700,3   Set(DIAL_ALLOW_CB=1)
4700,4   Set(DIAL_PRIORITY=0)
4700,5   Set(CHANNEL(hangup_handler_push)=dial-hangup,s,1)
4700,6   Set(CHANNEL(language)=en)
4700,7   Set(DIAL_TRUNK=4700)
4700,8   Dial(PJSIP/4700@demo-pbx,30,tT)
4700,9   GotoIf($["${DIALSTATUS}" = "BUSY"]?12)
4700,10  Playback(vm-nobodyavail)
4700,11  Hangup()
4700,12  Busy(10)
4700,13  Hangup()
_47XX,1  Set(__DIAL_EVENT=demo)
_47XX,2  Set(DIAL_EXTEN=${EXTEN})
...      (same preamble)
_47XX,7  Set(DIAL_TRUNK=4700)
_47XX,8  Dial(PJSIP/${EXTEN}@demo-pbx,30,tT)
...      (same busy / no-answer rows)
```

Asterisk prefers the exact match, so `4700` wins over `_47XX`; a number of the block that is registered as
a normal extension cannot exist (DIAL refuses both directions of that overlap). Without a bound SIP account
the rows play `vm-nobodyavail`. Trunks have no mailbox. `remove_extension` / `sync_event` prune the pattern
row together with the base (`dialplan.extens_for_extension`).

The route API answers for every number of an *active* block, so `[dial-internal]` → `[dial-lookup]` works
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
check that the asserted number lies inside `DIAL_TRUNK`'s block.

### GSM gateway trunk (`pjsip.conf`)

`conf/pjsip.conf` contains a **commented** `[gsm-gateway]` template (endpoint + aor + identify) for an
on-site cell network (e.g. `osmo-sip-connector` in front of `osmo-msc`). Uncomment it, set the gateway IP
in `contact=` / `match=`, and make sure the section name equals `Event.gsm_trunk` (default
`gsm-gateway`) for events with *has GSM* enabled. DIAL dials GSM devices as `PJSIP/<msisdn>@<gsm_trunk>`.
The GSM core links SIMs to DIAL devices by calling `POST /prov/gsm/register/` (header
`X-DIAL-PBX-Secret`, body `{event, token, imsi, msisdn?}`) - the same secret as the hooks below.

## Hooks and route API (Asterisk → DIAL)

All requests carry `X-DIAL-PBX-Secret: ${DIAL_PBX_HOOK_SECRET}`. DIAL checks it against the **event's**
hook secret (orga page `/e/<slug>/pbx/`, *PBX connection*) when one is saved, otherwise against the
server-wide `settings.DIAL_PBX_HOOK_SECRET` (falling back to `ASTERISK_ARI_PASSWORD`). A venue box
therefore serves exactly the events whose secret it knows. Form-encoded POST bodies via `CURL()`; responses are JSON
and parsed with `JSON_DECODE`. Timeouts are short (`[dial-curl]`) so a dead DIAL never blocks a call.

| Call | Where | Body / query | Uses response |
|---|---|---|---|
| `POST /api/v1/pbx/hooks/feature-code/` | `[dial-feature]`, `ringback-request` (`code=ringback`), `wakeup` (`code=wakeup`, `target=HHMM`) | `event, caller, code, target` | `handled` → beep+thank you or `pbx-invalid`. Handlers: callback (`*66`/`*86`), call groups (`*71`/`*72`), call forwarding (`*21<n>` always, `*22<n>` busy, `*23<n>` no answer, `*20` off - `apps.extensions.feature_codes`) |
| `POST /api/v1/pbx/hooks/dect-claim/` | `dect-claim` service | `event, caller` (PJSIP endpoint), `callerid, code` | `handled, number` (announced with `SayDigits`) |
| `POST /api/v1/pbx/hooks/announcement-record-start/` | `record-announcement` service | `event, caller` (PJSIP endpoint), `callerid, code` | `handled, number, name, file` (`file` = absolute path without extension to `Record()` into; `name` = its basename, used with `DIAL_RECORD_DIR` when set) |
| `POST /api/v1/pbx/hooks/announcement-recorded/` | `record-announcement` service / `[dial-record-hangup]` | `event, code, file` (`<file>.wav`), `duration` (s) | `handled, number` → thank you or `pbx-invalid`; DIAL imports the wav into `MEDIA_ROOT/ivr/<slug>/<number>/` when it can read it, else keeps the path as playback reference |
| `POST /api/v1/pbx/hooks/extension-idle/` | `[dial-hangup]` | `event, number, channel` | – |
| `POST /api/v1/pbx/hooks/cdr/` | `[dial-hangup]` if `DIAL_CDR_HOOK=yes` | `event, src, dst, start, answer, end, duration, billsec, disposition, channel, dstchannel, uniqueid, dcontext, accountcode` | – |
| `POST /api/v1/pbx/hooks/voicemail/` | `scripts/vm_notify.sh` (externnotify) | `event, mailbox, caller, file_path, duration, new_messages, old_messages` | – |
| `POST /api/v1/pbx/hooks/site-survey/` | `survey` service loop | `event, caller` | `say` (RFP name, spelled with `SayAlpha`) |
| `GET /api/v1/pbx/route/?event=&number=` | `[dial-lookup]` (groups, IVR, unknown numbers, emergency, trunk blocks, federation/breakout) | – | `type, dial_string, strategy, timeout, forward_busy, forward_noanswer, forward_unconditional, ivr_greeting, ivr_timeout, ivr_options`; groups additionally `waves, dial_string_waves, callerid_prefix`; trunk blocks additionally `trunk: {base, range}` |
| `GET /api/v1/pbx/dialplan/?shell=1` | `scripts/dial_contexts.sh` at load / `dialplan reload` | – | text: `[dial-<slug>]` shell contexts |

DIAL → Asterisk: ARI (`/ari/asterisk/info`, `/channels`, `/endpoints/PJSIP/..`, `/deviceStates/..`,
`/mailboxes/..`) and, as fallback for originate and for `dialplan reload` after `sync_event`, AMI.

## Environment variables (docker-compose)

| Variable | Default | Must match |
|---|---|---|
| `DIAL_API_URL` | `http://dial:8000` | DIAL web service URL reachable from the container (a venue box uses the public `https://` URL over the VPN) |
| `DIAL_PBX_HOOK_SECRET` | `dial` | the event's hook secret from `/e/<slug>/pbx/`, or DIAL's server-wide `DIAL_PBX_HOOK_SECRET` (or `ASTERISK_ARI_PASSWORD`) when the event has none |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `db` / `5432` / `dial` / `dial` / `dial` | DIAL's `DATABASE_URL` (same database!) in shared-database mode, the local `venue-db` in venue-agent mode. `DATABASE_*` accepted as aliases |
| `ARI_USER` / `ARI_PASSWORD` | `dial` / `dial` | DIAL `ASTERISK_ARI_USER` / `ASTERISK_ARI_PASSWORD` |
| `AMI_USER` / `AMI_PASSWORD` | `dial` / `dial` | DIAL `ASTERISK_AMI_USER` / `ASTERISK_AMI_PASSWORD` |
| `SIP_DOMAIN` | `dial.local` | DIAL `ASTERISK_SIP_DOMAIN` (digest realm, TLS cert CN) |
| `EXTERNAL_IP` | – | host LAN IP when using bridge networking (sets `external_*_address`); alias `SIP_EXTERNAL_IP` |
| `DIAL_EVENTS` | – | optional comma-separated slugs used for shell contexts when DIAL is unreachable at boot |
| `DIAL_EMERGENCY_FALLBACK` | – | optional number dialled when DIAL has no emergency route |
| `DIAL_CDR_HOOK` | `no` | `yes` to also POST CDRs to the hook (otherwise use the `ingest_cdrs` sweep). Required in venue-agent mode |
| `DIAL_RECORD_DIR` | – | optional override of the directory `record-announcement` writes to; by default the path from the `announcement-record-start` response (DIAL's `DIAL_RECORDING_DIR`, `/var/spool/asterisk/dial-recordings`) is used. Mount the same volume in DIAL (`recordings` in docker-compose) so the wav can be imported |
| `RTP_START` / `RTP_END` | `10000` / `10200` | published UDP port range |

DIAL side: `DIAL_PBX_BACKEND=apps.pbx.backends.asterisk.AsteriskPBX`, `ASTERISK_ARI_URL=http://asterisk:8088/ari`,
`ASTERISK_AMI_HOST=asterisk`.

Ports: 5060/udp+tcp (SIP), 5061/tcp (SIP TLS, self-signed), 8088/tcp (HTTP/ARI - keep internal),
8089/tcp (WSS scaffold), 5038/tcp (AMI - keep internal), 10000-10200/udp (RTP). On the event LAN
prefer `network_mode: host` - SIP/RTP through Docker NAT is painful.

Volumes: `/var/spool/asterisk/voicemail` (share with DIAL for `apps.voicemail`),
`/var/spool/asterisk/dial-recordings` (announcements recorded by phone - share with DIAL so `apps.ivr` can import them),
`/var/lib/asterisk/sounds/dial` (custom prompts `dial/<name>`; core sounds are used until recorded),
`/etc/asterisk/keys` (mount `asterisk.crt`, `asterisk.key`, `asterisk.pem` to replace the self-signed cert),
DIAL's `media` volume read-only at the path `render_musiconhold()` emits (custom ringback tones), and
`/etc/asterisk/dial-moh.conf` (rendered MOH classes, see above).

## Testing with a softphone

1. `manage.py migrate`, start the stack, check `docker compose logs asterisk` for
   `[dial] starting` and no ODBC errors; `docker compose exec asterisk asterisk -rx "odbc show"`.
2. In DIAL create an event (`demo`), approve an extension `4242` and add a **SIP** device to it
   (or `POST /api/v1/pbx/resync/?event=demo` as orga). Check the rows:
   `asterisk -rx "pjsip show endpoints"`, `asterisk -rx "dialplan show 4242@dial-demo"`,
   `asterisk -rx "realtime load ps_endpoints id <sip_username>"`.
3. Register a softphone (Linphone, Zoiper, Baresip …): server = container/host IP, user/password =
   `Device.sip_username` / `sip_password` (visible on the device page), transport UDP/TCP 5060 or
   TLS 5061 (accept the self-signed cert). `asterisk -rx "pjsip show contacts"` lists it.
4. Dial the plan's echo test number (`9003` in the demo plan) → echo; `9001` wake-up prompt;
   `*66<number>` → feature-code hook (needs `apps.callback`).
5. Dial a second extension for a real call; hang up and watch `dial.pbx.api` logs for the
   `extension-idle` hook; `select * from cdr order by id desc limit 1;` shows the CDR.
6. From DIAL: `get_pbx().health()`, `extension_status(ext)`, `originate(event=..., destination="4242",
   caller_id="DIAL", variables={"DIAL_SERVICE": "announce", "DIAL_ANNOUNCEMENT": "hello-world"})`.
7. `scripts/ari_smoke.sh` inside the container or `DIAL_INTEGRATION=1 pytest tests/integration` from
   the host (see `tests/integration/README.md`).

## Notes / limitations

- `DIAL_PBX_HOOK_SECRET` and passwords are pasted into config files: avoid `;`, `$`, `{`, `}` and
  newlines in them.
- Timestamps in `cdr` are written as UTC (`usegmtime=yes`) into `timestamptz` columns: run the
  database with `TimeZone=UTC` (the default in the official postgres image).
- WebRTC (`transport-wss`, 8089) is a scaffold: the endpoint options are set by `sync_device` for
  `webrtc` devices, but DIAL has no browser softphone yet.
- MWI: app_voicemail drives MWI from `voicemail_users`/spool directly; `res_ari_mailboxes` is
  `noload`ed so `AsteriskPBX.set_mwi` logs a warning and returns (by design).
- Provisioning pushes from DIAL arrive through the **PBX outbox** (`apps/pbx/outbox.py`): if Asterisk or
  the database is down, rows are written later with backoff instead of being lost; `dialplan reload`
  after `sync_event` still goes via AMI. Watch `GET /api/v1/pbx/outbox/?event=` when "nothing
  changes" on the PBX.
- `dial-moh.conf` is static: a tone that is *ready* in DIAL but has no class in Asterisk plays the
  normal ring. Re-render and `moh reload`.
