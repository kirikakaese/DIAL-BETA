"""LDAP phonebook server: BER codec, filters, directory entries, the asyncio server and ``ldapsearch``."""
import asyncio
import os
import shutil
import socket
import subprocess
import threading

import pytest

from apps.extensions.services import register
from apps.phonebook import services
from apps.phonebook.ldap import ber, directory, protocol
from apps.phonebook.ldap.server import LDAPServer

# --------------------------------------------------------------------------- BER


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 256, -1, -128, -129, 65535, 2**31 - 1, -(2**31), 2**40])
def test_ber_integer_round_trip(value):
    encoded = ber.encode_integer(value)
    tag, content = ber.decode(encoded)
    assert tag == ber.TAG_INTEGER and ber.decode_integer(content) == value
    assert ber.decode_integer(ber.decode(ber.encode_enumerated(value))[1]) == value


def test_ber_minimal_integer_encoding():
    assert ber.encode_integer(0) == b"\x02\x01\x00"
    assert ber.encode_integer(127) == b"\x02\x01\x7f"
    assert ber.encode_integer(128) == b"\x02\x02\x00\x80"
    assert ber.encode_integer(-1) == b"\x02\x01\xff"


def test_ber_strings_booleans_and_long_lengths():
    assert ber.decode(ber.encode_boolean(True)) == (ber.TAG_BOOLEAN, b"\xff")
    assert ber.decode_boolean(b"\x00") is False and ber.decode_boolean(b"\x01") is True
    assert ber.decode(ber.encode_octet_string("Zoë")) == (ber.TAG_OCTET_STRING, "Zoë".encode())
    long = b"x" * 300
    encoded = ber.encode_octet_string(long, tag=ber.context(5))
    assert encoded[:4] == bytes([0x85, 0x82, 0x01, 0x2C])
    assert ber.decode(encoded) == (ber.context(5), long)
    huge = b"y" * 70000
    assert ber.decode(ber.encode_octet_string(huge))[1] == huge


def test_ber_sequence_and_set_round_trip():
    seq = ber.encode_sequence(ber.encode_integer(7), ber.encode_octet_string("cn"),
                              ber.encode_set(ber.encode_octet_string("a"), ber.encode_octet_string("b")))
    tag, content = ber.decode(seq)
    assert tag == ber.TAG_SEQUENCE
    items = ber.decode_sequence(content)
    assert [t for t, _ in items] == [ber.TAG_INTEGER, ber.TAG_OCTET_STRING, ber.TAG_SET]
    assert ber.decode_integer(items[0][1]) == 7
    assert [v for _t, v in ber.decode_sequence(items[2][1])] == [b"a", b"b"]
    assert ber.application(3) == 0x63 and ber.application(2, constructed=False) == 0x42
    assert ber.context(0) == 0x80 and ber.context(0, True) == 0xA0


def test_ber_errors_and_framing():
    with pytest.raises(ber.BERError):
        ber.decode(b"\x30\x05\x02\x01")  # truncated content
    with pytest.raises(ber.BERError):
        ber.decode(b"\x30\x80\x00\x00\x00\x00")  # indefinite length
    with pytest.raises(ber.BERError):
        ber.decode(b"\x02\x01\x00\x00")  # trailing byte
    with pytest.raises(ber.BERError):
        ber.decode_integer(b"\x01" * 9)
    assert ber.pdu_length(b"") is None and ber.pdu_length(b"\x30") is None
    assert ber.pdu_length(b"\x30\x84") is None  # 4-byte length, header incomplete
    assert ber.pdu_length(b"\x30\x03\x02\x01") == 5
    assert ber.pdu_length(b"\x30\x82\x01\x2c" + b"\x00" * 10) == 4 + 300
    assert ber.pdu_length(b"\x30\x1f\x00\x00\x00\x00\x00") == 33
    with pytest.raises(ber.BERError):
        ber.pdu_length(b"\x30\x85\x00\x00\x00\x00\x00")  # 5-byte length


# --------------------------------------------------------------------------- filters

ALICE = {"objectClass": ["top", "inetOrgPerson"], "cn": ["alice"], "sn": ["alice"], "telephoneNumber": ["4242"],
         "mobile": ["4242"]}
BOB = {"objectClass": ["top", "inetOrgPerson"], "cn": ["Bob Builder"], "sn": ["Builder"], "givenName": ["Bob"],
       "telephoneNumber": ["4300"], "description": ["Hackcenter"]}
INFODESK = {"objectClass": ["top", "inetOrgPerson"], "cn": ["Infodesk"], "sn": ["Infodesk"],
            "telephoneNumber": ["4400"]}


def _matches(text):
    flt = protocol.parse_filter(text)
    return [e["cn"][0] for e in (ALICE, BOB, INFODESK) if protocol.evaluate(flt, e)]


def test_filter_equality_present_and_not():
    assert _matches("(cn=ALICE)") == ["alice"]  # case-insensitive
    assert _matches("(telephoneNumber=4300)") == ["Bob Builder"]
    assert _matches("(sn=nobody)") == []
    assert _matches("(givenName=*)") == ["Bob Builder"]
    assert _matches("(objectClass=*)") == ["alice", "Bob Builder", "Infodesk"]
    assert _matches("(!(givenName=*))") == ["alice", "Infodesk"]
    assert _matches("(&(objectClass=inetOrgPerson)(!(cn=alice)))") == ["Bob Builder", "Infodesk"]
    assert _matches("(cn~=bob builder)") == ["Bob Builder"]
    assert _matches("(telephoneNumber>=4000)") == []  # ordering matches are unsupported -> False


def test_filter_substrings():
    assert _matches("(cn=*foo*)") == []
    assert _matches("(cn=*BUILD*)") == ["Bob Builder"]
    assert _matches("(cn=bob*)") == ["Bob Builder"]
    assert _matches("(cn=*desk)") == ["Infodesk"]
    assert _matches("(cn=b*b*er)") == ["Bob Builder"]
    assert _matches("(cn=*o*d*)") == ["Bob Builder", "Infodesk"]
    assert _matches("(cn=a*a)") == []  # initial and final must not overlap: "alice" -> no
    assert _matches("(|(cn=*b*)(telephoneNumber=42*))") == ["alice", "Bob Builder"]
    assert _matches("(|(cn=*a*)(telephoneNumber=43*))") == ["alice", "Bob Builder"]
    assert _matches("(&(|(cn=*a*)(telephoneNumber=42*))(mobile=*))") == ["alice"]
    flt = protocol.parse_filter("(cn=Zo\\c3\\ab*)")
    assert isinstance(flt, protocol.Substrings) and flt.initial == "Zoë" and flt.final is None


def test_filter_string_parsing_and_ber_round_trip():
    text = "(&(|(cn=*a*)(telephoneNumber=42*))(!(sn=x))(givenName=*)(cn~=al)(mobile<=5))"
    flt = protocol.parse_filter(text)
    encoded = protocol.encode_filter(flt)
    tag, content = ber.decode(encoded)
    assert protocol.decode_filter(tag, content) == flt
    assert protocol.parse_filter("cn=alice") == protocol.Equality("cn", "alice")
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_filter("(cn=alice")
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_filter("(=x)")


# --------------------------------------------------------------------------- DNs and entries


def test_dn_helpers():
    esc = directory.escape_rdn_value
    assert esc("Bob, Jr. + Co") == "Bob\\, Jr. \\+ Co"
    assert esc(" lead") == "\\ lead" and esc("#hash") == "\\#hash" and esc("trail ") == "trail\\ "
    assert directory.parse_dn("CN=Bob\\, Jr.+telephoneNumber=4300, OU=Phonebook,dc=demo,dc=DIAL") == (
        frozenset({("dc", "dial")}), frozenset({("dc", "demo")}), frozenset({("ou", "phonebook")}),
        frozenset({("cn", "bob, jr."), ("telephonenumber", "4300")}))
    assert directory.parse_dn("cn=Bob\\2c Jr.,dc=demo,dc=dial") == directory.parse_dn("cn=Bob\\, Jr.,dc=demo,dc=dial")
    assert directory.parse_dn("") == ()
    assert directory.event_slug_from_dn("ou=phonebook,dc=demo,dc=dial") == "demo"
    assert directory.event_slug_from_dn("dc=demo,dc=dial") == "demo"
    assert directory.event_slug_from_dn("dc=dial") is None
    assert directory.event_slug_from_dn("ou=phonebook,dc=demo,dc=example") is None
    assert directory.bind_dn_slug("cn=directory,dc=demo,dc=dial") == "demo"
    assert directory.bind_dn_slug("cn=Directory, DC=demo, DC=dial") == "demo"
    assert directory.bind_dn_slug("cn=admin,dc=demo,dc=dial") is None
    assert directory.bind_dn_slug("cn=directory,ou=x,dc=demo,dc=dial") is None
    assert directory.bind_dn("demo") == "cn=directory,dc=demo,dc=dial"
    assert directory.split_name("Bob the Builder") == ("Bob the", "Builder")
    assert directory.split_name("alice") == ("", "alice")


@pytest.mark.django_db
def test_directory_entries(event, ext_alice, ext_bob, ext_group, ext_hidden):
    s = services.get_settings(event)
    s.categories = [{"name": "Services", "prefix": "44"}]
    s.save()
    ext_bob.description = "Ask for bricks"
    ext_bob.save()
    d = directory.load_event_directory("demo")
    assert d is not None and d.enabled and d.token == s.directory_token and d.name == "Demo Camp"
    dns = [e.dn for e in d.entries]
    assert dns[:2] == ["dc=demo,dc=dial", "ou=phonebook,dc=demo,dc=dial"]
    assert dns[2:] == [
        "cn=alice+telephoneNumber=4242,ou=phonebook,dc=demo,dc=dial",
        "cn=Bob Builder+telephoneNumber=4300,ou=phonebook,dc=demo,dc=dial",
        "cn=Infodesk+telephoneNumber=4400,ou=phonebook,dc=demo,dc=dial",
    ]
    alice, bob, desk = (e.attributes for e in d.entries[2:])
    assert alice["objectClass"] == ["top", "person", "organizationalPerson", "inetOrgPerson"]
    assert alice["cn"] == ["alice"] and alice["sn"] == ["alice"] and "givenName" not in alice
    assert alice["telephoneNumber"] == ["4242"] and alice["mobile"] == ["4242"] and alice["o"] == ["Demo Camp"]
    assert alice["description"] == ["Hackcenter, table 12"] and alice["l"] == ["Hackcenter, table 12"]
    assert bob["givenName"] == ["Bob"] and bob["sn"] == ["Builder"] and bob["displayName"] == ["Bob Builder"]
    assert bob["description"] == ["Ask for bricks"] and "ou" not in bob
    assert desk["ou"] == ["Services"] and desk["title"] == [str(ext_group.get_type_display())]
    # scopes / selection
    base = directory.parse_dn("ou=phonebook,dc=demo,dc=dial")
    assert [e.matches_scope(base, protocol.SCOPE_BASE) for e in d.entries] == [False, True, False, False, False]
    assert [e.matches_scope(base, protocol.SCOPE_ONE) for e in d.entries] == [False, False, True, True, True]
    assert sum(e.matches_scope(base, protocol.SCOPE_SUB) for e in d.entries) == 4
    assert d.entries[2].select(["CN", "mobile"]) == {"cn": ["alice"], "mobile": ["4242"]}
    assert d.entries[2].select(["1.1"]) == {}
    assert d.entries[2].select([], types_only=True)["cn"] == []
    found, matched = d.search("cn=nobody,ou=phonebook,dc=demo,dc=dial", protocol.SCOPE_BASE, lambda e: True)
    assert found is None and matched == "ou=phonebook,dc=demo,dc=dial"
    # privacy switch hides the location
    s.show_location = False
    s.save()
    alice = directory.load_event_directory("demo").entries[2].attributes
    assert "l" not in alice and "description" not in alice


@pytest.mark.django_db
def test_directory_visibility_and_cache(event, ext_alice):
    assert directory.load_event_directory("nope") is None
    assert directory.load_naming_contexts() == ["dc=demo,dc=dial"]
    s = services.get_settings(event)
    s.directory_enabled = False
    s.save()
    d = directory.load_event_directory("demo")
    assert d is not None and not d.enabled and d.entries == []
    assert directory.load_naming_contexts() == []
    s.directory_enabled = True
    s.save()
    event.state = event.State.DRAFT
    event.save()
    assert directory.load_event_directory("demo") is None
    calls = []

    def loader(slug):
        calls.append(slug)
        return directory.EventDirectory(slug, "x", "t", True, [])

    cache = directory.DirectoryCache(ttl=60)
    assert cache.get_event("demo", loader).name == "x"
    cache.get_event("demo", loader)
    assert calls == ["demo"]
    cache.clear()
    cache.get_event("demo", loader)
    assert calls == ["demo", "demo"]
    assert directory.DirectoryCache().ttl == 30


# --------------------------------------------------------------------------- server


class ServerThread:
    """Runs :class:`LDAPServer` on an ephemeral port in its own event-loop thread."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.port = None
        self.server = None
        self._ready = threading.Event()
        self._loop = None
        self._stop = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self.server = LDAPServer("127.0.0.1", 0, bind_failure_delay=0, **self.kwargs)
        await self.server.start()
        self.port = self.server.port
        self._ready.set()
        await self._stop.wait()
        await self.server.stop()

    def start(self):
        self._thread.start()
        assert self._ready.wait(10), "LDAP server did not start"
        return self

    def stop(self):
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(10)


class Client:
    """Blocking test client speaking our own PDU encoders."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.buf = b""
        self.mid = 0

    def close(self):
        self.sock.close()

    def send_raw(self, data: bytes):
        self.sock.sendall(data)

    def send(self, builder, *args, **kwargs) -> int:
        self.mid += 1
        self.sock.sendall(builder(self.mid, *args, **kwargs))
        return self.mid

    def recv(self) -> protocol.Response:
        while True:
            need = ber.pdu_length(self.buf)
            if need is not None and len(self.buf) >= need:
                pdu, self.buf = self.buf[:need], self.buf[need:]
                return protocol.parse_response(pdu)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk

    def bind(self, dn="", password="") -> protocol.Response:
        mid = self.send(protocol.bind_request, dn, password)
        resp = self.recv()
        assert resp.message_id == mid and resp.op_tag == protocol.BIND_RESPONSE
        return resp

    def search(self, base, flt="(objectClass=*)", scope=protocol.SCOPE_SUB, **kwargs):
        mid = self.send(protocol.search_request, base, flt, scope=scope, **kwargs)
        entries = []
        while True:
            resp = self.recv()
            assert resp.message_id == mid
            if resp.op_tag == protocol.SEARCH_RESULT_DONE:
                return entries, resp
            assert resp.op_tag == protocol.SEARCH_RESULT_ENTRY
            entries.append(resp)


@pytest.fixture
def start_server():
    started = []

    def _start(**kwargs):
        srv = ServerThread(**kwargs).start()
        started.append(srv)
        return srv

    yield _start
    for srv in started:
        srv.stop()


@pytest.fixture
def ldap_server(start_server):
    return start_server(allow_anonymous=False)


@pytest.fixture
def client(ldap_server):
    c = Client(ldap_server.port)
    yield c
    c.close()


@pytest.fixture
def token(event):
    return services.get_settings(event).directory_token


BIND_DN = "cn=directory,dc=demo,dc=dial"
BASE = "ou=phonebook,dc=demo,dc=dial"
server_db = pytest.mark.django_db(transaction=True)


@server_db
def test_server_bind(client, event, ext_alice, token):
    assert client.bind(BIND_DN, "wrong-password").result_code == protocol.INVALID_CREDENTIALS
    assert client.bind("cn=directory,dc=other,dc=dial", token).result_code == protocol.INVALID_CREDENTIALS
    assert client.bind("cn=admin,dc=demo,dc=dial", token).result_code == protocol.INVALID_CREDENTIALS
    anon = client.bind("", "")
    assert anon.result_code == protocol.INAPPROPRIATE_AUTHENTICATION and "anonymous" in anon.message
    assert client.bind(BIND_DN, "").result_code == protocol.UNWILLING_TO_PERFORM
    ok = client.bind(BIND_DN, token)
    assert ok.result_code == protocol.SUCCESS and ok.message == ""
    assert client.bind("CN=Directory, DC=demo, DC=dial", token).result_code == protocol.SUCCESS
    # LDAPv2 is refused
    client.mid += 1
    client.send_raw(protocol.bind_request(client.mid, BIND_DN, token, version=2))
    assert client.recv().result_code == protocol.PROTOCOL_ERROR


@server_db
def test_server_search_sub_with_filters(client, event, ext_alice, ext_bob, ext_group, ext_hidden, token):
    assert client.bind(BIND_DN, token).result_code == protocol.SUCCESS
    entries, done = client.search(BASE, "(cn=*b*)")
    assert done.result_code == protocol.SUCCESS
    assert [e.dn for e in entries] == ["cn=Bob Builder+telephoneNumber=4300,ou=phonebook,dc=demo,dc=dial"]
    assert entries[0].attributes["telephoneNumber"] == ["4300"] and entries[0].attributes["givenName"] == ["Bob"]
    entries, done = client.search(BASE, "(|(cn=*a*)(telephoneNumber=44*))", attributes=["cn", "telephoneNumber"])
    assert done.result_code == protocol.SUCCESS
    assert [(e.attributes["cn"][0], e.attributes["telephoneNumber"][0]) for e in entries] == [
        ("alice", "4242"), ("Infodesk", "4400")]
    assert set(entries[0].attributes) == {"cn", "telephoneNumber"}
    # Yealink-style query on several attributes, hidden extension never shows up
    entries, _done = client.search(BASE, "(|(cn=*43*)(sn=*43*)(telephoneNumber=*43*)(mobile=*43*))")
    assert [e.attributes["telephoneNumber"][0] for e in entries] == ["4300"]
    entries, _done = client.search(BASE, "(objectClass=inetOrgPerson)")
    assert [e.attributes["telephoneNumber"][0] for e in entries] == ["4242", "4300", "4400"]
    # scopes
    entries, done = client.search(BASE, scope=protocol.SCOPE_BASE)
    assert done.result_code == 0 and [e.dn for e in entries] == [BASE]
    assert entries[0].attributes["objectClass"] == ["top", "organizationalUnit"]
    entries, _done = client.search(BASE, scope=protocol.SCOPE_ONE)
    assert len(entries) == 3 and all(e.attributes["objectClass"][-1] == "inetOrgPerson" for e in entries)
    entries, _done = client.search("dc=demo,dc=dial")
    assert len(entries) == 5 and entries[0].dn == "dc=demo,dc=dial"
    entries, done = client.search("cn=alice+telephoneNumber=4242,ou=phonebook,dc=demo,dc=dial",
                                  scope=protocol.SCOPE_BASE, types_only=True)
    assert done.result_code == 0 and entries[0].attributes["cn"] == []
    # unknown base below a known event -> noSuchObject with the nearest matched DN
    entries, done = client.search("ou=nothing,dc=demo,dc=dial")
    assert entries == [] and done.result_code == protocol.NO_SUCH_OBJECT and done.matched_dn == "dc=demo,dc=dial"


@server_db
def test_server_size_limit_and_access(client, event, ext_alice, ext_bob, ext_group, token):
    # unbound + anonymous disabled -> no access
    entries, done = client.search(BASE)
    assert entries == [] and done.result_code == protocol.INSUFFICIENT_ACCESS_RIGHTS
    client.bind(BIND_DN, token)
    entries, done = client.search(BASE, "(objectClass=inetOrgPerson)", size_limit=2)
    assert len(entries) == 2 and done.result_code == protocol.SIZE_LIMIT_EXCEEDED
    entries, done = client.search(BASE, "(objectClass=inetOrgPerson)", size_limit=3)
    assert len(entries) == 3 and done.result_code == protocol.SUCCESS
    # bound to demo -> other events are off limits; foreign suffixes do not exist
    _e, done = client.search("ou=phonebook,dc=other,dc=dial")
    assert done.result_code == protocol.INSUFFICIENT_ACCESS_RIGHTS
    _e, done = client.search("dc=example,dc=org")
    assert done.result_code == protocol.NO_SUCH_OBJECT
    # write operations are declined, not fatal
    client.mid += 1
    delete = ber.encode_sequence(ber.encode_integer(client.mid),
                                 ber.encode_octet_string(BASE, tag=ber.application(protocol.DEL_REQUEST, False)))
    client.send_raw(delete)
    resp = client.recv()
    assert resp.op_tag == 11 and resp.result_code == protocol.UNWILLING_TO_PERFORM
    # WhoAmI / StartTLS / unknown extended
    client.send(protocol.extended_request, protocol.OID_WHOAMI)
    resp = client.recv()
    assert resp.op_tag == protocol.EXTENDED_RESPONSE and resp.result_code == 0
    assert resp.value == b"dn:" + BIND_DN.encode()
    client.send(protocol.extended_request, protocol.OID_START_TLS)
    assert client.recv().result_code == protocol.PROTOCOL_ERROR
    client.send(protocol.extended_request, "1.2.3.4")
    assert client.recv().result_code == protocol.PROTOCOL_ERROR
    client.send(protocol.abandon_request, 99)  # silently ignored
    _e, done = client.search(BASE, scope=protocol.SCOPE_BASE)
    assert done.result_code == 0


@server_db
def test_server_root_dse_and_unbind(client, event, ext_alice, token):
    entries, done = client.search("", scope=protocol.SCOPE_BASE)
    assert done.result_code == 0 and len(entries) == 1 and entries[0].dn == ""
    dse = entries[0].attributes
    assert dse["supportedLDAPVersion"] == ["3"] and dse["vendorName"] == ["DIAL"]
    assert dse["namingContexts"] == []  # not bound, anonymous disabled -> nothing to reveal
    assert client.bind(BIND_DN, token).result_code == 0
    entries, _done = client.search("", scope=protocol.SCOPE_BASE, attributes=["namingContexts"])
    assert entries[0].attributes == {"namingContexts": ["dc=demo,dc=dial"]}
    entries, done = client.search("", "(cn=alice)")  # sub from the root covers the bound event
    assert done.result_code == 0 and [e.dn for e in entries] == [
        "cn=alice+telephoneNumber=4242,ou=phonebook,dc=demo,dc=dial"]
    client.send(protocol.unbind_request)
    with pytest.raises(ConnectionError):
        client.recv()


@server_db
def test_server_bad_pdu_closes_connection(ldap_server, event):
    c = Client(ldap_server.port)
    c.send_raw(b"\x30\x84\x7f\xff\xff\xff" + b"\x00" * 8)  # 2 GiB PDU announced
    notice = c.recv()
    assert notice.message_id == 0 and notice.name == protocol.OID_NOTICE_OF_DISCONNECTION
    with pytest.raises(ConnectionError):
        c.recv()
    c.close()
    c = Client(ldap_server.port)
    c.send_raw(b"\x04\x03abc")  # complete PDU, but not an LDAPMessage
    notice = c.recv()
    assert notice.name == protocol.OID_NOTICE_OF_DISCONNECTION and notice.result_code == protocol.PROTOCOL_ERROR
    with pytest.raises(ConnectionError):
        c.recv()
    c.close()
    # the server is still alive afterwards
    c = Client(ldap_server.port)
    assert c.search("", scope=protocol.SCOPE_BASE)[1].result_code == 0
    c.close()


@server_db
def test_server_anonymous_and_disabled_events(start_server, event, ext_alice, token):
    srv = start_server(allow_anonymous=True, cache=directory.DirectoryCache(ttl=0))
    c = Client(srv.port)
    assert c.bind("", "").result_code == 0
    entries, done = c.search(BASE, "(telephoneNumber=4242)")
    assert done.result_code == 0 and len(entries) == 1
    dse, _done = c.search("", scope=protocol.SCOPE_BASE)
    assert dse[0].attributes["namingContexts"] == ["dc=demo,dc=dial"]
    _e, done = c.search("ou=phonebook,dc=unknown,dc=dial")
    assert done.result_code == protocol.NO_SUCH_OBJECT
    s = services.get_settings(event)
    s.directory_enabled = False
    s.save()
    _e, done = c.search(BASE)
    assert done.result_code == protocol.NO_SUCH_OBJECT
    assert c.bind(BIND_DN, token).result_code == protocol.INVALID_CREDENTIALS
    s.directory_enabled = True
    s.save()
    event.state = event.State.ARCHIVED
    event.save()
    assert c.bind(BIND_DN, token).result_code == protocol.INVALID_CREDENTIALS
    c.close()


@server_db
def test_server_unicode_and_escaped_dns(client, event, user, token):
    register(event, user, "4711", "dect", display_name="Zoë Ünïcode, Jr.")
    client.bind(BIND_DN, token)
    entries, done = client.search(BASE, "(cn=zoë*)")
    assert done.result_code == 0 and len(entries) == 1
    dn = entries[0].dn
    assert dn == "cn=Zoë Ünïcode\\, Jr.+telephoneNumber=4711,ou=phonebook,dc=demo,dc=dial"
    assert entries[0].attributes["sn"] == ["Jr."] and entries[0].attributes["givenName"] == ["Zoë Ünïcode,"]
    entries, done = client.search(dn, scope=protocol.SCOPE_BASE)
    assert done.result_code == 0 and len(entries) == 1
    entries, done = client.search(dn.replace("\\,", "\\2c"), scope=protocol.SCOPE_BASE)
    assert done.result_code == 0 and len(entries) == 1


def test_management_command_defaults(settings):
    from django.core.management import get_commands

    assert get_commands()["dial_ldap"] == "apps.phonebook"
    assert settings.DIAL_LDAP_PORT == 3890 and settings.DIAL_LDAP_HOST == "0.0.0.0"
    assert settings.DIAL_LDAP_ALLOW_ANONYMOUS is False and settings.DIAL_LDAP_CACHE_SECONDS == 30


@server_db
@pytest.mark.skipif(shutil.which("ldapsearch") is None, reason="OpenLDAP client not installed")
def test_ldapsearch_integration(ldap_server, event, ext_alice, ext_bob, ext_group, token):
    env = {**os.environ, "LDAPNOINIT": "1"}
    url = f"ldap://127.0.0.1:{ldap_server.port}"

    def run(*args, password=token):
        cmd = ["ldapsearch", "-x", "-o", "nettimeout=10", "-o", "ldif-wrap=no", "-LLL", "-H", url]
        if password is not None:
            cmd += ["-D", BIND_DN, "-w", password]
        return subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=30, env=env)

    res = run("-b", BASE, "(cn=*)")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "dn: cn=Bob Builder+telephoneNumber=4300,ou=phonebook,dc=demo,dc=dial" in out
    assert "telephoneNumber: 4242" in out and "cn: Infodesk" in out and "sn: Builder" in out
    assert out.count("objectClass: inetOrgPerson") == 3
    res = run("-b", BASE, "-z", "1", "(objectClass=inetOrgPerson)", "telephoneNumber")
    assert res.returncode == 4 and "telephoneNumber: 4242" in res.stdout  # sizeLimitExceeded
    assert run("-b", BASE, "(cn=*)", password="nope").returncode == 49
    # ldapsearch always binds - without -D that is an anonymous bind, which is refused by default
    res = run("-b", "", "-s", "base", "(objectClass=*)", "vendorName", password=None)
    assert res.returncode == 48 and "anonymous bind is disabled" in res.stderr
    res = run("-b", "", "-s", "base", "(objectClass=*)", "vendorName", "namingContexts")
    assert res.returncode == 0 and "vendorName: DIAL" in res.stdout
    assert "namingContexts: dc=demo,dc=dial" in res.stdout
