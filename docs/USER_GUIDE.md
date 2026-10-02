# PET User Guide

Welcome! This guide explains how to get a phone number at an event that runs PET, connect a DECT handset
or a softphone, and use the services on the network. Numbers and codes below are the defaults
(`9000`, `*66`, ...) - your event's dashboard shows the actual ones.

## 1. Account & login

- **Register** at `/accounts/register/` with e-mail, a public nickname and a password. Your account is
  global: one login for every event that uses this PET server.
  - Depending on how the server is configured you either sign up in one step and then receive a
    **verification mail**, or you enter your e-mail address first, click the link in the mail
    (valid for 48 hours by default) and then choose nickname and password. If the address already
    has an account you get a "you already have an account" mail instead of an error.
  - Until you have clicked the link your profile shows an **unverified** badge with a *Resend* button
    (`/accounts/profile/verify/resend/`). Verification mails are limited to a few per hour per address.
    On servers that require verification (the default) a banner reminds you on every page and you
    cannot register extensions until the address is confirmed.
- **Login** at `/accounts/login/`. Forgot your password? `/accounts/password/reset/`. After several
  wrong passwords the login is paused for a few minutes (the page tells you how long) - resetting the
  password works regardless.
- **Logging in with SSO.** If the server offers single sign-on, the login page shows an extra button
  (usually *Log in with SSO*) that takes you to your organisation's identity provider and back. Coming
  back the first time, PET links you to your existing account when the e-mail address matches and is
  verified - otherwise it creates a new account for you. On some servers SSO is the *only* way in and
  the password form is hidden. You can **link** or **unlink** SSO yourself on your profile page; to
  unlink you need a password set, so you are never locked out.
- Signup and login forms include invisible spam protection. If you see *Spam protection triggered* or
  *That was quick*, simply wait a moment and submit again; browser extensions that auto-fill every
  field can cause it.
- **Profile** (`/accounts/profile/`): display name, password change,
  API tokens (for the tech-minded), and your data export/deletion.
- **Changing your e-mail** (`/accounts/profile/email/`) always goes through a confirmation link sent to
  the *new* address; the old address is notified about the change.
- The interface follows your system's dark mode. It is English only; the language of the *voice prompts*
  callers hear (voicemail, announcements) is a per-event / per-extension setting, see section 6.

## 2. Joining an event

Open the event page `/e/<slug>/` (e.g. `/e/demo/`) and click **Join**. If the orga gave you a **group
code** (e.g. for angels/volunteers), enter it on the join page - it may unlock reserved number ranges. You
can join several events; use the event switcher in the navigation.

### Finding your way around

- The **top bar** is global: *My extensions* (all your numbers across events), *Events*, the event
  you are currently in with its state badge (*Live*, *Registration*, … - hover it for the dates), and
  your account menu (profile, API tokens, log out). The ◐ button toggles dark/light mode.
- Inside an event, the **sidebar** on the left lists everything for that event: *Event* (overview,
  phonebook, register a number), *My stuff* (your extensions, handsets, call groups, callbacks &
  wake-up, voicemail, messages, …) and — if you are on the orga team — the orange *Orga* sections.
  On phones the sidebar hides behind the ☰ button.

## 3. Picking a number

Go to **My extensions → New** (`/e/<slug>/extensions/new/`).

1. Choose the type: **DECT handset**, **SIP endpoint** (softphone/desk phone), **SIP trunk** (a whole
   block of numbers for your own PBX, see below) or others the orga enabled.
2. Type your desired number. The **availability checker** answers while you type: free / taken /
   needs approval / not allowed (and why: wrong length, reserved range, blocked prefix...).
   - Most events use **prefix-free numbering**: no number may be the beginning of another one, so
     `23` and `2323` cannot coexist (otherwise the phone system would have to wait after you dial
     `23` to see whether more digits follow). The checker tells you exactly which existing number
     is in the way.
   - No preference? Click **🎲 Give me a random free number** - the form is filled with a number you
     can register instantly.
3. If it's taken you'll see **suggestions**, and you can join the **waitlist** - you get a notification
   when it frees up.
4. Fill in display name (shown as caller-ID), an optional location hint ("Hackcenter, table 12") and decide
   whether you want to be listed in the **phonebook**.
5. Submit. Depending on the number plan your extension is **active immediately** or goes to the orga's
   **approval queue** (vanity numbers like `7777` and premium ranges usually do). You'll be notified when
   it is approved or rejected.

There is a quota of extensions per person per event; the checker tells you when you hit it.

**Reserved for you?** The orga can reserve a specific number for you (e.g. an infodesk or team
number). You then receive an invitation link (`/e/<slug>/numbering/claim/<token>/`); log in, confirm,
and the extension is active immediately - even if the number sits in a range you normally couldn't
register in. Claims expire (typically after 14 days), so redeem them in time.

## 4. DECT handset subscription

### The quick way: dial to claim (if your event has it)

If the orga enabled *dial-to-claim*, your extension page shows a box **"Claim a handset by dialing"** with a
number like `9004 123456` (the event's claim number followed by your personal **claim code**). Then:

1. Subscribe **any** handset to the event's DECT network. Pool handsets from the DECT desk are already
   subscribed; for your own handset ask the desk for the pool PIN (or the OMM accepts the event-wide code).
   Until claimed the handset shows a hint like *Dial 9004+code* and a temporary number.
2. Dial the number from the claim box from that handset. PET answers, binds the handset to your
   extension, renumbers it on the DECT system and reads your number back to you. Done - no IPEI typing.
3. Dialing the code from a different handset **moves** your number to that handset. Anyone who knows the
   code can do this, so keep it private; **New claim code** on the extension page invalidates the old one.

You can also dial just the claim number and enter the code when prompted.

### The classic way: add the IPEI yourself

1. Open your extension and **add a device → DECT**. Enter the **IPEI** of your handset - a 13-digit code
   printed under the battery / on the label of the handset, or shown in the handset menu (usually
   *Settings → Status* or by typing `*#06#`). On Mitel/Aastra handsets: *Menu → System → IPEI*.
   PET recognises the manufacturer from the first digits of the IPEI; if it shows *unknown vendor* you
   can suggest the right one on the device page (the list is crowdsourced and curated by the orga).
   Used a handset at an earlier event on this server? **My handsets** (`/e/<slug>/devices/history/`)
   lists them by IPEI with a **Reuse in this event** button that copies IPEI, model and label.
2. PET creates the subscription on the DECT system and shows you a **PIN** (access code). The PIN is valid
   for **30 minutes**; if it expires, click *New PIN* (or ask the helpdesk).
3. On the handset open the subscription menu (*Menu → Settings → System → Subscription* or similar),
   pick a free system slot, choose the event's DECT network when found, and enter the PIN.
4. After a few seconds the handset shows your number. The device page changes from *Pending subscription*
   to **Subscribed** once PET has seen the handset on a base station (up to 30 s).

"Subscribed" means the DECT system knows your handset and it is registered to the phone system - you can
make and receive calls. The device page also shows the last base station (RFP) you were seen on.

## 5. Softphone setup via QR

1. Add a device → **SIP**. PET generates a username and a long random password.
2. Install a softphone. The device page (**Set up your softphone**) offers one QR code per app family -
   scan the one for yours and the account is created without typing anything:

   | Your app | What to do |
   |---|---|
   | **Linphone** | Assistant → *Fetch remote configuration* → scan. (The code is a `linphone-config:` link to a configuration PET serves for your device.) |
   | **Groundwire / Cloud Softphone / Acrobits Softphone** | Settings → *Scan QR code*. |
   | **Other softphone (SIP URI)** | Grandstream Wave and other clients that accept a `sip:` URI with credentials. |
   | **Zoiper, desk phones, anything else** | Use the **Manual settings** card: server, port, username, password (reveal button), transport, display name. Zoiper's QR setup needs Zoiper's paid service, so PET does not offer it. |

   Every code contains your SIP password (or a link that serves it) - don't post screenshots.
3. Transport UDP/TCP (5060) or TLS (5061, accept the event's certificate).
4. Once registered the device state becomes **Registered**. Dial `9003` (echo test) to check audio.

Desk phones (Snom/Yealink/Grandstream/Cisco) can be autoprovisioned: when the orga has attached a
provisioning profile to your device, the device page shows a **provisioning URL**
(`https://<pet>/prov/<token>/<filename>`) to enter in the phone's web interface. Treat that URL like a
password - it contains your SIP credentials. Phones that only know their MAC address can fetch
`/prov/<vendor>/<mac>.cfg` (or `.xml`) instead, but must authenticate with the SIP username and
password (HTTP Basic) or the `?token=` query parameter.

### GSM handsets (if the event runs a cell network)

Some events operate their own small GSM network. If the orga enabled it, **GSM** appears as a device
type: add one at `/e/<slug>/devices/gsm/` with the SIM's IMSI (and optionally its MSISDN) and tick the
network generations (2G-5G) the SIM may use. PET shows a **6-digit one-time registration code**;
register on the cell network as instructed on site (dial or text the code) and the SIM is linked to
your device. From then on your extension rings on the phone.

## 6. Multi-device ringing

One extension can have several devices (a handset in your pocket and a softphone on the laptop). On the
extension page choose **Ring all at once** (parallel) or **one after another** (serial, with per-device
order and delay).

### Call forwarding

Every extension can forward to another extension of the same event (extension **Edit** page):

| Mode | Behaviour |
|---|---|
| Off | no forwarding (the legacy free-text busy/no-answer/always fields still apply if you filled them) |
| Always | callers go straight to the target; your own devices don't ring |
| Delayed | your devices ring for *N* seconds, then the call is forwarded |
| When busy | forward only when you are on another call |
| When unanswered | forward after your ring timeout |

Pick the target from your own active extensions or type any active number of the event. PET refuses
forwarding to yourself, to numbers that aren't active and to loops (A → B → A). If the target is later
deleted, expires or gets rejected, your forwarding is switched off automatically and you can see that
in the audit trail. The target's page lists who forwards to it under **Forwarded from**.

**From the phone** (no browser needed - the codes are the event's defaults, the dashboard shows the
real ones): `*21<number>` forward always, `*22<number>` when busy, `*23<number>` when unanswered,
`*20` switch every forwarding off. You hear a beep and "thank you" when it worked, "invalid" when the
target is not an active number or would create a loop. Phone and web change the same setting and both
are audited.

### Custom ringback tone

Instead of the standard ring, callers can hear your own sound while your devices ring. On the
extension's Edit page upload a **WAV, MP3, OGG or FLAC** file (max. 5 MB). PET checks the file and
converts it in the background to telephone format (8 kHz mono); the *Ringback tone* row on the extension
page shows **Processing**, **Ready** (with a player) or **Failed** with the reason. Tick **Clear** to go
back to the normal ring. Only you (the owner) and the orga can change it.

### Other per-extension settings

- **Call waiting** - off means a second caller hears busy instead of knocking on your active call.
- **Caller-ID display** - what the called party sees: number + name (`4242 Alice`, default), name only
  or number only.
- **DECT encryption** - encrypt the DECT air interface for handsets on this extension (DECT only;
  whether it takes effect depends on the DECT system and handset).
- **Announcement language** - English, German or the event default; the Asterisk sound pack used for
  voicemail prompts and system announcements callers of your number hear (the web UI is always English).

## 7. Phonebook

`/e/<slug>/phonebook/` lists everyone who opted in (**Public phonebook entry** on the extension), with
search. Exports: PDF (classic printed phonebook), CSV, vCard, LDIF. Your entry is your choice - untick
the box any time. Each row has a small vCard link to save that one contact. Depending on how the orga set
up the venue, desk phones and DECT handsets can show the same phonebook **directly on the device** -
look for a *Directory* key or menu and type the first letters of a name; the phone asks PET and lists
the matching entries to dial.

**Your business card.** The extension page has a **Business card** card: a QR code that *contains* your
contact (name, number, event) - any phone camera offers "add contact", no internet needed - plus
*Download vCard* and *Print card* (a credit-card sized page in the event's colours, `/e/<slug>/phonebook/<number>/card/`).
If your extension is hidden from the phonebook, only you and the orga can open these links.

## 8. Services you can dial

| Dial | What happens |
|---|---|
| `9000` | **Test ringback** - hang up, PET calls you back after ~10 s. Great for testing coverage and audio. |
| `9001` | **Wake-up service** - follow the prompt (enter `HHMM`), or schedule at `/e/<slug>/callback/wakeup/new/` with retries, repeat and a custom announcement. Snooze from the web. |
| `9003` | Echo test. |
| `9004<code>` | **Claim a DECT handset** for the extension whose claim code you dial (section 4). |
| `9005<code>` | **Record an announcement** by phone (if the event set a recording number): the code is on your announcement's page; speak after the beep, `#` to finish, the recording is played back and replaces the current audio. |
| `9999` | Voicemail - listen to your messages with your mailbox PIN. |
| `*66<number>` | **Callback** (CCBS/CCNR): the target was busy or didn't answer - PET calls you back when it becomes free and connects you. Requests expire after 30 minutes. |
| `*86` | Cancel your pending callbacks. |
| `*71` / `*72` | **Log in / out** of your call groups (helpdesk, medics, security...) from your own handset. |
| `*21<number>` / `*22<number>` / `*23<number>` / `*20` | **Call forwarding** always / busy / unanswered / off (section 6). |
| `112` / `110` | Emergency numbers - routed to on-site medics/security. Only in real emergencies. |

All of these are the defaults - the orga can change every number and code; the event dashboard
(*Dialing help*) shows the ones that apply to your event.

Callbacks can also be requested and cancelled at `/e/<slug>/callback/`.

### Call groups

A call group is a number that rings several people (helpdesk, medics, security...). You get into a
group either because a group manager adds your extension, or because they **invite** you: you receive
an e-mail and accept or decline under **My call groups → invitations**
(`/e/<slug>/callgroups/mine/`, `/e/<slug>/callgroups/invites/`). You can leave a group with your own
extension at any time. When the group rings your handset, the caller name is prefixed with the group's
**shortcode** (e.g. `[SEC] Alice`) so you know which group is calling. Group owners can appoint
**group admins** who manage members and settings without owning the number, give each member a **ring
delay** (escalation tiers in ring-all mode: first tier rings immediately, second tier after *N*
seconds) and even add another group as a member (nested groups, up to three levels).

## 9. Voicemail

Enable a mailbox on your extension (`/e/<slug>/voicemail/`): set a 4-6 digit **PIN**, optionally upload a
greeting and enable **e-mail delivery**. New messages light the **message-waiting** indicator on your
handset / softphone. Listen on the web (`/e/<slug>/voicemail/`, with play, mark read, delete) or by
dialling `9999`.

## 10. Messaging (if enabled)

`/e/<slug>/messaging/` lets you send short text messages to DECT handsets (they appear on the handset
display) and receive messages from the web gateway. Orga can broadcast to groups ("shift change in 10
minutes").

## 11. Transferring an extension

On the extension page click **Transfer**, enter the recipient's e-mail or nickname. They receive a link
(`/transfer/<token>/`) and must accept before it expires. Ownership, devices and settings move together;
the transfer is recorded in the audit log.

## 12. Your number next year

**Numbers belong to an event, never to a person.** Every event starts empty: you register again, the
new event's number plan decides, and nobody - not even orga - can reserve a number permanently across
events. What PET offers is convenience, not a right: when the next event opens registration, its
dashboard shows **Port your numbers** listing extensions you held before. One click *re-requests* the
same number through the normal rules (a number that became reserved needs approval; one somebody else
registered first is simply gone). Orga can pre-reserve a number for a specific person *within* one
event with a **claim** (invite link), which again lives and dies with that event. Archived events stay
browsable so you can look things up.

## 13. Privacy & your data

- Your phonebook entry is opt-in; location hint and display name are only shown if you choose to be listed.
- Call records: the orga decides between aggregate-only statistics and per-call records with a retention
  period. You can see and download what exists about your numbers at `/e/<slug>/stats/mine/` and delete it.
- Full account export: `/accounts/profile/export/` (JSON). Account deletion: `/accounts/profile/delete/`
  - extensions are released, your data is erased after the event's retention period.
- Helpdesk staff can look up your extensions to help you; every such lookup is audit-logged.

## 14. Install PET on your phone

PET works as an installable web app, so the event dashboard and the phonebook are one tap away - and
still open when the venue Wi-Fi drops.

- **Android / Chrome, Edge:** open PET and tap **Install PET on this device** at the bottom of the page
  (or browser menu → *Install app* / *Add to Home screen*).
- **iPhone / iPad (Safari):** *Share* → **Add to Home Screen**.
- **Desktop (Chrome, Edge):** click the install icon in the address bar.

Once installed, PET opens full-screen with its own icon. Pages you have visited - your event dashboard,
the phonebook list and the docs - are kept for offline use; a bar at the bottom tells you when you are
offline. Anything that changes data (registering a number, editing a device) needs a connection. Login,
QR codes, SIP passwords and orga pages are never stored on the device. When a new PET version is
deployed, a small "PET was updated" toast appears - tap **Reload**.

## 15. Accessibility

- **Keyboard:** everything works with Tab / Shift+Tab, Enter and Space; *Escape* closes menus and the
  event sidebar. The first Tab on every page reveals a *Skip to main content* link.
- **Screen readers:** pages have landmarks (navigation, main, footer), one heading per page, labelled
  form fields with errors announced, and status messages that are read out when they appear.
- **Motion & contrast:** PET follows your operating system - *reduce motion* switches off animations,
  *increase contrast* thickens borders and focus rings. Dark and light theme both meet WCAG AA contrast.
- **Zoom:** the layout reflows up to 200 % zoom and on 320 px wide screens; tables scroll sideways
  instead of overflowing.
- Status is never shown by colour alone - badges carry ✓ / ! / ✕ glyphs.

If something is hard to use, tell the helpdesk or the event's orga; they can pass it on to the operator.
