"""Asterisk realtime tables + DIAL-side bookkeeping.

The ``Ps*``, ``Extension``-dialplan, ``VoicemailUser`` and ``Cdr`` models are
*managed* Django models whose ``db_table`` names match the Asterisk realtime
schema (``contrib/ast-db-manage/config``), so Asterisk reads/writes the DIAL
database directly:

==================  =========================  ==================================
Asterisk table      DIAL model                  Consumer (Asterisk side)
==================  =========================  ==================================
ps_endpoints        PsEndpoint                 res_pjsip via sorcery realtime
ps_auths            PsAuth                     res_pjsip
ps_aors             PsAor                      res_pjsip
ps_contacts         PsContact                  res_pjsip (written by Asterisk)
ps_endpoint_id_ips  PsEndpointIdIp             res_pjsip_endpoint_identifier_ip
extensions          DialplanEntry              pbx_realtime (``switch => Realtime/@``)
voicemail_users     VoicemailUser              app_voicemail realtime
cdr                 Cdr                        cdr_adaptive_odbc
==================  =========================  ==================================

Column names must be valid option names of the respective Asterisk object -
sorcery refuses to load rows with unknown columns - so only add columns you
have checked against the Asterisk version you deploy. Boolean-ish options are
stored as ``yes``/``no`` strings exactly like the alembic enum columns.
"""
import uuid

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

YESNO = [("yes", "yes"), ("no", "no")]


def _yesno(default=None, **kw):
    return models.CharField(max_length=5, choices=YESNO, null=True, blank=True, default=default, **kw)


class PsEndpoint(models.Model):
    """``ps_endpoints`` - one row per SIP account (DIAL ``Device``)."""

    id = models.CharField(max_length=40, primary_key=True)
    transport = models.CharField(max_length=40, null=True, blank=True)
    aors = models.CharField(max_length=200, null=True, blank=True)
    auth = models.CharField(max_length=100, null=True, blank=True)
    context = models.CharField(max_length=40, null=True, blank=True)
    disallow = models.CharField(max_length=200, null=True, blank=True)
    allow = models.CharField(max_length=200, null=True, blank=True)
    direct_media = _yesno()
    callerid = models.CharField(max_length=40, null=True, blank=True)
    callerid_privacy = models.CharField(max_length=40, null=True, blank=True)
    mailboxes = models.CharField(max_length=40, null=True, blank=True)
    dtmf_mode = models.CharField(max_length=20, null=True, blank=True)
    rewrite_contact = _yesno()
    rtp_symmetric = _yesno()
    force_rport = _yesno()
    ice_support = _yesno()
    media_encryption = models.CharField(max_length=20, null=True, blank=True)
    media_use_received_transport = _yesno()
    use_avpf = _yesno()
    webrtc = _yesno()
    dtls_auto_generate_cert = _yesno()
    send_pai = _yesno()
    send_rpid = _yesno()
    send_diversion = _yesno()
    trust_id_inbound = _yesno()
    trust_id_outbound = _yesno()
    device_state_busy_at = models.IntegerField(null=True, blank=True)
    language = models.CharField(max_length=40, null=True, blank=True)
    set_var = models.TextField(null=True, blank=True)
    identify_by = models.CharField(max_length=80, null=True, blank=True)
    from_user = models.CharField(max_length=40, null=True, blank=True)
    from_domain = models.CharField(max_length=40, null=True, blank=True)
    outbound_auth = models.CharField(max_length=40, null=True, blank=True)
    accountcode = models.CharField(max_length=80, null=True, blank=True)
    moh_suggest = models.CharField(max_length=40, null=True, blank=True)
    allow_subscribe = _yesno()
    named_call_group = models.CharField(max_length=40, null=True, blank=True)
    named_pickup_group = models.CharField(max_length=40, null=True, blank=True)
    subscribe_context = models.CharField(max_length=40, null=True, blank=True)
    message_context = models.CharField(max_length=40, null=True, blank=True)
    aggregate_mwi = _yesno()
    mwi_subscribe_replaces_unsolicited = _yesno()
    tos_audio = models.CharField(max_length=10, null=True, blank=True)
    cos_audio = models.IntegerField(null=True, blank=True)
    allow_transfer = _yesno()
    inband_progress = _yesno()
    timers = models.CharField(max_length=10, null=True, blank=True)

    class Meta:
        db_table = "ps_endpoints"
        verbose_name = _("PJSIP endpoint")

    def __str__(self):
        return self.id


class PsAuth(models.Model):
    """``ps_auths`` - SIP digest credentials."""

    id = models.CharField(max_length=40, primary_key=True)
    auth_type = models.CharField(max_length=10, default="userpass",
                                 choices=[("userpass", "userpass"), ("md5", "md5")])
    nonce_lifetime = models.IntegerField(null=True, blank=True)
    md5_cred = models.CharField(max_length=40, null=True, blank=True)
    password = models.CharField(max_length=128, null=True, blank=True)
    realm = models.CharField(max_length=255, null=True, blank=True)
    username = models.CharField(max_length=40, null=True, blank=True)

    class Meta:
        db_table = "ps_auths"
        verbose_name = _("PJSIP auth")

    def __str__(self):
        return self.id


class PsAor(models.Model):
    """``ps_aors`` - address of record (where registrations land)."""

    id = models.CharField(max_length=40, primary_key=True)
    contact = models.CharField(max_length=255, null=True, blank=True)
    default_expiration = models.IntegerField(null=True, blank=True)
    mailboxes = models.CharField(max_length=80, null=True, blank=True)
    max_contacts = models.IntegerField(null=True, blank=True)
    minimum_expiration = models.IntegerField(null=True, blank=True)
    maximum_expiration = models.IntegerField(null=True, blank=True)
    remove_existing = _yesno()
    remove_unavailable = _yesno()
    qualify_frequency = models.IntegerField(null=True, blank=True)
    qualify_timeout = models.FloatField(null=True, blank=True)
    authenticate_qualify = _yesno()
    support_path = _yesno()
    outbound_proxy = models.CharField(max_length=255, null=True, blank=True)
    voicemail_extension = models.CharField(max_length=40, null=True, blank=True)

    class Meta:
        db_table = "ps_aors"
        verbose_name = _("PJSIP AOR")

    def __str__(self):
        return self.id


class PsContact(models.Model):
    """``ps_contacts`` - registrations, written by Asterisk (``contact=realtime``)."""

    id = models.CharField(max_length=255, primary_key=True)
    uri = models.CharField(max_length=511, null=True, blank=True)
    expiration_time = models.BigIntegerField(null=True, blank=True)
    qualify_frequency = models.IntegerField(null=True, blank=True)
    qualify_timeout = models.FloatField(null=True, blank=True)
    authenticate_qualify = _yesno()
    outbound_proxy = models.CharField(max_length=255, null=True, blank=True)
    path = models.TextField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, null=True, blank=True)
    reg_server = models.CharField(max_length=255, null=True, blank=True)
    via_addr = models.CharField(max_length=40, null=True, blank=True)
    via_port = models.IntegerField(null=True, blank=True)
    call_id = models.CharField(max_length=255, null=True, blank=True)
    endpoint = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    prune_on_boot = _yesno()

    class Meta:
        db_table = "ps_contacts"
        verbose_name = _("PJSIP contact")

    def __str__(self):
        return self.id


class PsEndpointIdIp(models.Model):
    """``ps_endpoint_id_ips`` - identify an endpoint by source IP (trunks, OMM)."""

    id = models.CharField(max_length=40, primary_key=True)
    endpoint = models.CharField(max_length=40, null=True, blank=True)
    match = models.CharField(max_length=80, null=True, blank=True)
    srv_lookups = _yesno()
    match_header = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "ps_endpoint_id_ips"
        verbose_name = _("PJSIP IP identify")

    def __str__(self):
        return self.id


class DialplanEntry(models.Model):
    """``extensions`` - realtime dialplan rows consumed by pbx_realtime."""

    context = models.CharField(max_length=40)
    exten = models.CharField(max_length=40)
    priority = models.IntegerField()
    app = models.CharField(max_length=40)
    appdata = models.CharField(max_length=1024, blank=True, default="")

    class Meta:
        db_table = "extensions"
        unique_together = [("context", "exten", "priority")]
        ordering = ["context", "exten", "priority"]
        verbose_name = _("dialplan entry")
        verbose_name_plural = _("dialplan entries")

    def __str__(self):
        return f"[{self.context}] {self.exten},{self.priority},{self.app}({self.appdata})"

    def as_conf_line(self) -> str:
        return f"exten => {self.exten},{self.priority},{self.app}({self.appdata})"


class VoicemailUser(models.Model):
    """``voicemail_users`` - app_voicemail realtime mailboxes."""

    uniqueid = models.AutoField(primary_key=True)
    context = models.CharField(max_length=80)
    mailbox = models.CharField(max_length=80)
    password = models.CharField(max_length=80, blank=True, default="")
    fullname = models.CharField(max_length=80, blank=True, default="")
    email = models.CharField(max_length=80, blank=True, default="")
    pager = models.CharField(max_length=80, blank=True, default="")
    attach = _yesno()
    attachfmt = models.CharField(max_length=10, null=True, blank=True)
    serveremail = models.CharField(max_length=80, null=True, blank=True)
    language = models.CharField(max_length=20, null=True, blank=True)
    tz = models.CharField(max_length=30, null=True, blank=True)
    deletevoicemail = _yesno()
    saycid = _yesno()
    sendvoicemail = _yesno()
    review = _yesno()
    tempgreetwarn = _yesno()
    operator = _yesno()
    envelope = _yesno()
    sayduration = _yesno()
    forcename = _yesno()
    forcegreetings = _yesno()
    callback = models.CharField(max_length=80, null=True, blank=True)
    dialout = models.CharField(max_length=80, null=True, blank=True)
    exitcontext = models.CharField(max_length=80, null=True, blank=True)
    maxmsg = models.IntegerField(null=True, blank=True)
    volgain = models.FloatField(null=True, blank=True)
    stamp = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "voicemail_users"
        unique_together = [("context", "mailbox")]
        verbose_name = _("voicemail mailbox")

    def __str__(self):
        return f"{self.mailbox}@{self.context}"


class Cdr(models.Model):
    """``cdr`` - call detail records inserted by cdr_adaptive_odbc.

    The alembic schema has no primary key; Django needs one, so ``id`` is a
    serial column Asterisk never touches (it only fills columns that match CDR
    variables). ``end`` is a reserved word - set ``quoted_identifiers`` in
    ``cdr_adaptive_odbc.conf``.
    """

    accountcode = models.CharField(max_length=20, null=True, blank=True)
    src = models.CharField(max_length=80, null=True, blank=True, db_index=True)
    dst = models.CharField(max_length=80, null=True, blank=True, db_index=True)
    dcontext = models.CharField(max_length=80, null=True, blank=True)
    clid = models.CharField(max_length=80, null=True, blank=True)
    channel = models.CharField(max_length=80, null=True, blank=True)
    dstchannel = models.CharField(max_length=80, null=True, blank=True)
    lastapp = models.CharField(max_length=80, null=True, blank=True)
    lastdata = models.CharField(max_length=80, null=True, blank=True)
    start = models.DateTimeField(null=True, blank=True, db_index=True)
    answer = models.DateTimeField(null=True, blank=True)
    end = models.DateTimeField(null=True, blank=True)
    duration = models.IntegerField(null=True, blank=True)
    billsec = models.IntegerField(null=True, blank=True)
    disposition = models.CharField(max_length=45, null=True, blank=True)
    amaflags = models.CharField(max_length=45, null=True, blank=True)
    userfield = models.CharField(max_length=256, null=True, blank=True)
    uniqueid = models.CharField(max_length=150, null=True, blank=True, db_index=True)
    linkedid = models.CharField(max_length=150, null=True, blank=True)
    peeraccount = models.CharField(max_length=20, null=True, blank=True)
    sequence = models.IntegerField(null=True, blank=True)
    # DIAL-side: set once apps.stats has ingested the row (via the ``cdr`` hook or a sweep)
    ingested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "cdr"
        ordering = ["-start"]
        verbose_name = _("CDR")
        verbose_name_plural = _("CDRs")

    def __str__(self):
        return f"{self.src} -> {self.dst} {self.start:%Y-%m-%d %H:%M} {self.disposition}" if self.start \
            else f"{self.src} -> {self.dst}"

    def as_record(self) -> dict:
        """Shape consumed by ``apps.stats.services.ingest_cdr``."""
        return {
            "src": self.src, "dst": self.dst, "start": self.start, "answer": self.answer, "end": self.end,
            "duration": self.duration, "billsec": self.billsec, "disposition": self.disposition,
            "channel": self.channel, "dstchannel": self.dstchannel, "uniqueid": self.uniqueid,
            "rfp": None, "dcontext": self.dcontext, "linkedid": self.linkedid,
        }


class PBXSyncLog(models.Model):
    """DIAL-side audit of provisioning pushes to the PBX (one row per sync call)."""

    class Kind(models.TextChoices):
        EXTENSION = "extension", _("Extension")
        DEVICE = "device", _("Device")
        EVENT = "event", _("Event")
        REMOVE = "remove", _("Remove")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    event = models.ForeignKey("events.Event", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="pbx_sync_logs")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    target = models.CharField(max_length=120, blank=True)
    ok = models.BooleanField(default=True)
    message = models.TextField(blank=True)
    rows = models.PositiveIntegerField(default=0, help_text=_("Realtime rows written."))

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("PBX sync log")

    def __str__(self):
        return f"{self.created_at:%H:%M:%S} {self.kind} {self.target} {'ok' if self.ok else 'FAILED'}"


class PBXJob(models.Model):
    """Durable outbox entry for one PBX adapter call (see :mod:`apps.pbx.outbox`).

    Producers enqueue a job instead of calling the adapter directly; ``drain_outbox`` (beat) and
    ``enqueue_and_drain`` (kick after commit) deliver it with retries and exponential backoff.
    ``sync_*``/``remove_*`` jobs for the same target are coalesced via ``dedupe_key`` while pending.
    """

    class Kind(models.TextChoices):
        SYNC_EXTENSION = "sync_extension", _("Sync extension")
        REMOVE_EXTENSION = "remove_extension", _("Remove extension")
        SYNC_DEVICE = "sync_device", _("Sync device")
        REMOVE_DEVICE = "remove_device", _("Remove device")
        SYNC_EVENT = "sync_event", _("Sync event")
        SET_MWI = "set_mwi", _("Set MWI")
        ORIGINATE = "originate", _("Originate call")
        BROADCAST = "broadcast", _("Broadcast")
        HANGUP = "hangup", _("Hangup")
        CUSTOM = "custom", _("Custom")

    class State(models.TextChoices):
        PENDING = "pending", _("Pending")
        SENDING = "sending", _("Sending")
        DELIVERED = "delivered", _("Delivered")
        FAILED = "failed", _("Failed")
        DEAD = "dead", _("Dead")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey("events.Event", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="pbx_jobs")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    target_type = models.CharField(max_length=20, blank=True, help_text=_("extension / device / event"))
    target_id = models.CharField(max_length=64, blank=True)
    payload = models.JSONField(default=dict, blank=True, help_text=_("Keyword arguments for the adapter call."))
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error = models.TextField(blank=True)
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    priority = models.SmallIntegerField(default=0, help_text=_("Higher runs first."))

    class Meta:
        ordering = ["-priority", "next_attempt_at"]
        verbose_name = _("PBX job")

    def __str__(self):
        return f"{self.kind} {self.target_type}:{self.target_id} [{self.state}]"

    @property
    def is_open(self) -> bool:
        return self.state in (self.State.PENDING, self.State.SENDING)


# --------------------------------------------------------------------------- per-event venue PBX

PBX_BACKEND_LABELS = {
    "asterisk": "Asterisk (ARI/AMI + realtime)",
    "dummy": _("Simulator - no real PBX"),
}


def pbx_backend_choices():
    from django.conf import settings

    return [(k, PBX_BACKEND_LABELS.get(k, k)) for k in getattr(settings, "DIAL_PBX_BACKENDS", {})]


class PBXConnection(models.Model):
    """How DIAL reaches *this event's* PBX at the venue.

    DIAL is one permanent service; every event brings its own phone infrastructure. This row holds the
    adapter and credentials for the event's Asterisk (ARI for originate/channels, AMI as fallback and
    for reloads). Realtime provisioning goes through the shared database by default - the venue
    Asterisk reads its ``ps_*``/dialplan rows from DIAL's PostgreSQL. In ``agent`` mode a small venue
    agent pulls snapshots of those rows over HTTPS (:mod:`apps.pbx.snapshot`) into a local database
    instead and reports back with heartbeats (the ``agent_*`` fields). Events without a row fall back
    to the server-wide ``ASTERISK`` settings from ``.env``.
    """

    class Provisioning(models.TextChoices):
        SHARED_DB = "shared_db", _("Shared database – venue Asterisk reads DIAL's PostgreSQL")
        AGENT = "agent", _("Venue agent – pulls snapshots over HTTPS into a local database")

    AGENT_STALE_FACTOR = 3  # no heartbeat within this many poll intervals -> stale

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="pbx_connection")
    backend = models.CharField(max_length=40, default="asterisk",
                               help_text=_("Adapter key from DIAL_PBX_BACKENDS."))
    provisioning = models.CharField(
        _("Provisioning"), max_length=12, choices=Provisioning.choices, default=Provisioning.SHARED_DB,
        help_text=_("How the realtime rows reach the venue Asterisk."))
    agent_poll_interval = models.PositiveIntegerField(
        _("Agent poll interval"), default=15, validators=[MinValueValidator(5)],
        help_text=_("Seconds between snapshot polls of the venue agent."))
    agent_last_seen = models.DateTimeField(_("Agent last seen"), null=True, blank=True)
    agent_version = models.CharField(_("Applied snapshot version"), max_length=64, blank=True)
    agent_host = models.CharField(_("Agent host"), max_length=200, blank=True)
    agent_software = models.CharField(_("Agent software"), max_length=64, blank=True)
    agent_asterisk_ok = models.BooleanField(_("Agent reports Asterisk ok"), null=True, blank=True)
    agent_message = models.CharField(_("Agent message"), max_length=500, blank=True)
    ari_url = models.URLField(_("ARI URL"), blank=True, help_text=_("e.g. http://10.20.0.5:8088/ari"))
    ari_user = models.CharField(_("ARI user"), max_length=64, blank=True)
    ari_password = models.CharField(_("ARI password"), max_length=128, blank=True)
    ari_app = models.CharField(_("ARI application"), max_length=64, blank=True, default="dial")
    ami_host = models.CharField(_("AMI host"), max_length=200, blank=True,
                                help_text=_("Empty = AMI disabled (ARI only)."))
    ami_port = models.PositiveIntegerField(_("AMI port"), default=5038)
    ami_user = models.CharField(_("AMI user"), max_length=64, blank=True)
    ami_password = models.CharField(_("AMI password"), max_length=128, blank=True)
    hook_secret = models.CharField(
        _("Hook secret"), max_length=128, blank=True,
        help_text=_("Sent by the venue Asterisk as X-DIAL-PBX-Secret on hooks, route lookups and phone "
                    "provisioning. Empty = server-wide DIAL_PBX_HOOK_SECRET."))
    notes = models.TextField(blank=True, help_text=_("Where the box is, who to call, VPN details..."))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("PBX connection")

    def __str__(self):
        return f"{self.event.slug}: {self.backend} {self.ari_url or self.ami_host or '-'}"

    # ------------------------------------------------------------------ venue agent
    @property
    def is_agent(self) -> bool:
        return self.provisioning == self.Provisioning.AGENT

    @property
    def agent_is_stale(self) -> bool:
        """No heartbeat within ``AGENT_STALE_FACTOR`` poll intervals (or never)."""
        if self.agent_last_seen is None:
            return True
        limit = self.AGENT_STALE_FACTOR * max(int(self.agent_poll_interval or 15), 1)
        return (timezone.now() - self.agent_last_seen).total_seconds() > limit

    @property
    def agent_behind(self) -> bool:
        """The agent's applied snapshot version differs from what DIAL would serve right now."""
        from apps.pbx.snapshot import snapshot_version

        return self.agent_version != snapshot_version(self.event)

    @property
    def backend_path(self) -> str:
        from django.conf import settings

        try:
            return settings.DIAL_PBX_BACKENDS[self.backend]
        except KeyError as exc:
            raise ValueError(f"Unknown PBX backend {self.backend!r}") from exc

    @property
    def backend_label(self) -> str:
        return str(PBX_BACKEND_LABELS.get(self.backend, self.backend))

    def config(self) -> dict:
        """``settings.ASTERISK``-shaped dict; ``SIP_DOMAIN`` follows the event."""
        from django.conf import settings

        base = dict(getattr(settings, "ASTERISK", {}) or {})
        # agent mode: an empty ARI URL means "no ARI" (the venue box is normally not reachable from DIAL),
        # not "use the server default"
        ari_url = self.ari_url or ("" if self.is_agent else base.get("ARI_URL", ""))
        return {
            "ARI_URL": ari_url,
            "ARI_USER": self.ari_user or base.get("ARI_USER", ""),
            "ARI_PASSWORD": self.ari_password or base.get("ARI_PASSWORD", ""),
            "ARI_APP": self.ari_app or base.get("ARI_APP", "dial"),
            "AMI_HOST": self.ami_host,
            "AMI_PORT": self.ami_port or 5038,
            "AMI_USER": self.ami_user,
            "AMI_PASSWORD": self.ami_password,
            "SIP_DOMAIN": self.event.sip_domain or base.get("SIP_DOMAIN", ""),
            "PROVISIONING": self.provisioning,
            "EVENT_ID": self.event_id,
        }
