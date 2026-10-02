"""LDAP v3 message codec (RFC 4511) and search filters (RFC 4511 §4.5.1 / RFC 4515 strings).

Only the operations a read-only directory needs are modelled in detail: Bind, Unbind, Search, Abandon and
Extended requests are parsed; other write operations are recognised by tag so the server can decline them
with ``unwillingToPerform``. Response builders cover BindResponse, SearchResultEntry, SearchResultDone,
ExtendedResponse and a generic LDAPResult. The request builders at the bottom form a tiny client used by the
tests (and handy for debugging from a shell).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ber

# application tags (RFC 4511 §4.1.1 protocolOp)
BIND_REQUEST = 0
BIND_RESPONSE = 1
UNBIND_REQUEST = 2
SEARCH_REQUEST = 3
SEARCH_RESULT_ENTRY = 4
SEARCH_RESULT_DONE = 5
MODIFY_REQUEST = 6
ADD_REQUEST = 8
DEL_REQUEST = 10
MODIFY_DN_REQUEST = 12
COMPARE_REQUEST = 14
ABANDON_REQUEST = 16
EXTENDED_REQUEST = 23
EXTENDED_RESPONSE = 24
# request tag -> response tag for operations that get a plain LDAPResult back
WRITE_OPERATIONS = {MODIFY_REQUEST: 7, ADD_REQUEST: 9, DEL_REQUEST: 11, MODIFY_DN_REQUEST: 13,
                    COMPARE_REQUEST: 15}

# result codes
SUCCESS = 0
OPERATIONS_ERROR = 1
PROTOCOL_ERROR = 2
TIME_LIMIT_EXCEEDED = 3
SIZE_LIMIT_EXCEEDED = 4
AUTH_METHOD_NOT_SUPPORTED = 7
NO_SUCH_OBJECT = 32
INAPPROPRIATE_AUTHENTICATION = 48
INVALID_CREDENTIALS = 49
INSUFFICIENT_ACCESS_RIGHTS = 50
UNAVAILABLE = 52
UNWILLING_TO_PERFORM = 53

# search scopes
SCOPE_BASE = 0
SCOPE_ONE = 1
SCOPE_SUB = 2

# well-known extended operation OIDs
OID_START_TLS = "1.3.6.1.4.1.1466.20037"
OID_WHOAMI = "1.3.6.1.4.1.4203.1.11.3"
OID_NOTICE_OF_DISCONNECTION = "1.3.6.1.4.1.1466.20036"

MAX_PDU_SIZE = 1024 * 1024


class ProtocolError(ValueError):
    """Well-formed BER that is not a valid LDAP message."""


# --------------------------------------------------------------------------- filters

@dataclass(frozen=True)
class And:
    items: tuple

    def __call__(self, entry):
        return all(f(entry) for f in self.items)


@dataclass(frozen=True)
class Or:
    items: tuple

    def __call__(self, entry):
        return any(f(entry) for f in self.items)


@dataclass(frozen=True)
class Not:
    item: object

    def __call__(self, entry):
        return not self.item(entry)


def _values(entry: dict, attr: str) -> list[str]:
    attr = attr.lower()
    for name, vals in entry.items():
        if name.lower() == attr:
            return [str(v).lower() for v in vals]
    return []


@dataclass(frozen=True)
class Equality:
    attr: str
    value: str

    def __call__(self, entry):
        return self.value.lower() in _values(entry, self.attr)


@dataclass(frozen=True)
class Approx(Equality):
    """approxMatch - phones rarely send it; treated like a case-insensitive equality."""


@dataclass(frozen=True)
class Substrings:
    attr: str
    initial: str | None = None
    any: tuple = ()
    final: str | None = None

    def _match(self, value: str) -> bool:
        pos = 0
        if self.initial is not None:
            if not value.startswith(self.initial.lower()):
                return False
            pos = len(self.initial)
        for part in self.any:
            idx = value.find(part.lower(), pos)
            if idx < 0:
                return False
            pos = idx + len(part)
        if self.final is not None:
            final = self.final.lower()
            return len(value) - pos >= len(final) and value.endswith(final)
        return True

    def __call__(self, entry):
        return any(self._match(v) for v in _values(entry, self.attr))


@dataclass(frozen=True)
class Present:
    attr: str

    def __call__(self, entry):
        return bool(_values(entry, self.attr))


@dataclass(frozen=True)
class Unsupported:
    """greaterOrEqual / lessOrEqual / extensibleMatch: no ordering rules here, so they never match."""

    kind: str
    attr: str = ""

    def __call__(self, entry):
        return False


def evaluate(flt, entry: dict) -> bool:
    """Evaluate a filter AST against ``{attribute: [values]}`` (case-insensitive names and values)."""
    return bool(flt(entry))


def _decode_substrings(attr: str, content: bytes) -> Substrings:
    initial = final = None
    anys = []
    for tag, value in ber.decode_sequence(content):
        text = ber.decode_string(value)
        kind = tag & 0x1F
        if kind == 0 and initial is None:
            initial = text
        elif kind == 1:
            anys.append(text)
        elif kind == 2 and final is None:
            final = text
        else:
            raise ProtocolError("malformed substrings filter")
    return Substrings(attr, initial, tuple(anys), final)


def _decode_ava(content: bytes) -> tuple[str, str]:
    items = ber.decode_sequence(content)
    if len(items) != 2:
        raise ProtocolError("malformed AttributeValueAssertion")
    return ber.decode_string(items[0][1]), ber.decode_string(items[1][1])


def decode_filter(tag: int, content: bytes, depth: int = 0):
    """Filter CHOICE -> AST (see module doc). ``depth`` guards against pathological nesting."""
    if depth > 32:
        raise ProtocolError("filter nested too deeply")
    if (tag & 0xC0) != ber.CLASS_CONTEXT:
        raise ProtocolError(f"unexpected filter tag {tag:#x}")
    kind = tag & 0x1F
    if kind in (0, 1):
        items = tuple(decode_filter(t, c, depth + 1) for t, c in ber.decode_sequence(content))
        return And(items) if kind == 0 else Or(items)
    if kind == 2:
        items = ber.decode_sequence(content)
        if len(items) != 1:
            raise ProtocolError("NOT filter needs exactly one operand")
        return Not(decode_filter(items[0][0], items[0][1], depth + 1))
    if kind == 3:
        return Equality(*_decode_ava(content))
    if kind == 4:
        items = ber.decode_sequence(content)
        if len(items) != 2:
            raise ProtocolError("malformed substrings filter")
        return _decode_substrings(ber.decode_string(items[0][1]), items[1][1])
    if kind == 5:
        return Unsupported("greaterOrEqual", _decode_ava(content)[0])
    if kind == 6:
        return Unsupported("lessOrEqual", _decode_ava(content)[0])
    if kind == 7:
        return Present(ber.decode_string(content))
    if kind == 8:
        return Approx(*_decode_ava(content))
    if kind == 9:
        return Unsupported("extensibleMatch")
    raise ProtocolError(f"unknown filter choice {kind}")


def encode_filter(flt) -> bytes:
    """AST -> BER (client side)."""
    if isinstance(flt, And):
        return ber.encode_sequence(*(encode_filter(f) for f in flt.items), tag=ber.context(0, True))
    if isinstance(flt, Or):
        return ber.encode_sequence(*(encode_filter(f) for f in flt.items), tag=ber.context(1, True))
    if isinstance(flt, Not):
        return ber.encode_sequence(encode_filter(flt.item), tag=ber.context(2, True))
    if isinstance(flt, Approx):
        return ber.encode_sequence(ber.encode_octet_string(flt.attr), ber.encode_octet_string(flt.value),
                                   tag=ber.context(8, True))
    if isinstance(flt, Equality):
        return ber.encode_sequence(ber.encode_octet_string(flt.attr), ber.encode_octet_string(flt.value),
                                   tag=ber.context(3, True))
    if isinstance(flt, Substrings):
        parts = []
        if flt.initial is not None:
            parts.append(ber.encode_octet_string(flt.initial, tag=ber.context(0)))
        parts += [ber.encode_octet_string(a, tag=ber.context(1)) for a in flt.any]
        if flt.final is not None:
            parts.append(ber.encode_octet_string(flt.final, tag=ber.context(2)))
        return ber.encode_sequence(ber.encode_octet_string(flt.attr), ber.encode_sequence(*parts),
                                   tag=ber.context(4, True))
    if isinstance(flt, Present):
        return ber.encode_octet_string(flt.attr, tag=ber.context(7))
    if isinstance(flt, Unsupported) and flt.kind in ("greaterOrEqual", "lessOrEqual"):
        kind = 5 if flt.kind == "greaterOrEqual" else 6
        return ber.encode_sequence(ber.encode_octet_string(flt.attr), ber.encode_octet_string(""),
                                   tag=ber.context(kind, True))
    raise ProtocolError(f"cannot encode filter {flt!r}")


# --------------------------------------------------------------------------- RFC 4515 filter strings

def _unescape(text: str) -> str:
    out = bytearray()
    i = 0
    raw = text.encode("utf-8")
    while i < len(raw):
        if raw[i] == 0x5C:  # backslash: RFC 4515 only allows \XX hex escapes
            hexpart = raw[i + 1:i + 3]
            try:
                if len(hexpart) != 2:
                    raise ValueError
                out.append(int(hexpart, 16))
            except ValueError:
                raise ProtocolError(f"bad escape in filter {text!r}")
            i += 3
            continue
        out.append(raw[i])
        i += 1
    return out.decode("utf-8", errors="replace")


class _FilterParser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def parse(self):
        flt = self._filter()
        if self.pos != len(self.text):
            raise ProtocolError(f"trailing characters in filter {self.text!r}")
        return flt

    def _expect(self, ch: str):
        if self.pos >= len(self.text) or self.text[self.pos] != ch:
            raise ProtocolError(f"expected {ch!r} at {self.pos} in {self.text!r}")
        self.pos += 1

    def _filter(self):
        self._expect("(")
        if self.pos >= len(self.text):
            raise ProtocolError("unterminated filter")
        ch = self.text[self.pos]
        if ch in "&|":
            self.pos += 1
            items = []
            while self.pos < len(self.text) and self.text[self.pos] == "(":
                items.append(self._filter())
            self._expect(")")
            return And(tuple(items)) if ch == "&" else Or(tuple(items))
        if ch == "!":
            self.pos += 1
            item = self._filter()
            self._expect(")")
            return Not(item)
        flt = self._item()
        self._expect(")")
        return flt

    def _item(self):
        end = self.text.find(")", self.pos)
        if end < 0:
            raise ProtocolError("unterminated filter item")
        body = self.text[self.pos:end]
        self.pos = end
        for op in ("~=", ">=", "<=", "="):
            idx = body.find(op)
            if idx > 0:
                attr, value = body[:idx], body[idx + len(op):]
                break
        else:
            raise ProtocolError(f"missing '=' in filter item {body!r}")
        attr = attr.strip()
        if op == "~=":
            return Approx(attr, _unescape(value))
        if op == ">=":
            return Unsupported("greaterOrEqual", attr)
        if op == "<=":
            return Unsupported("lessOrEqual", attr)
        if value == "*":
            return Present(attr)
        if "*" in value:
            parts = [_unescape(p) for p in value.split("*")]
            initial = parts[0] or None
            final = parts[-1] or None
            anys = tuple(p for p in parts[1:-1] if p)
            return Substrings(attr, initial, anys, final)
        return Equality(attr, _unescape(value))


def parse_filter(text: str):
    """RFC 4515 string (``(&(objectClass=person)(cn=*bob*))``) -> filter AST."""
    text = text.strip()
    if not text.startswith("("):
        text = f"({text})"
    return _FilterParser(text).parse()


# --------------------------------------------------------------------------- requests

@dataclass
class BindRequest:
    version: int
    name: str
    password: bytes | None  # None -> SASL (unsupported)
    sasl_mechanism: str = ""


@dataclass
class UnbindRequest:
    pass


@dataclass
class SearchRequest:
    base: str
    scope: int
    deref_aliases: int
    size_limit: int
    time_limit: int
    types_only: bool
    filter: object
    attributes: list[str] = field(default_factory=list)


@dataclass
class AbandonRequest:
    message_id: int


@dataclass
class ExtendedRequest:
    name: str
    value: bytes | None = None


@dataclass
class UnsupportedRequest:
    tag: int  # application tag number


@dataclass
class LDAPMessage:
    message_id: int
    op: object
    controls: bytes | None = None


def _parse_bind(content: bytes) -> BindRequest:
    items = ber.decode_sequence(content)
    if len(items) != 3:
        raise ProtocolError("malformed BindRequest")
    version = ber.decode_integer(items[0][1])
    name = ber.decode_string(items[1][1])
    auth_tag, auth = items[2]
    if auth_tag == ber.context(0):
        return BindRequest(version, name, auth)
    if auth_tag == ber.context(3, True):
        mech = ber.decode_sequence(auth)
        mechanism = ber.decode_string(mech[0][1]) if mech else ""
        return BindRequest(version, name, None, mechanism)
    raise ProtocolError(f"unknown authentication choice {auth_tag:#x}")


def _parse_search(content: bytes) -> SearchRequest:
    items = ber.decode_sequence(content)
    if len(items) != 8:
        raise ProtocolError("malformed SearchRequest")
    scope = ber.decode_integer(items[1][1])
    if scope not in (SCOPE_BASE, SCOPE_ONE, SCOPE_SUB):
        raise ProtocolError(f"invalid scope {scope}")
    ftag, fcontent = items[6]
    attrs = [ber.decode_string(v) for _t, v in ber.decode_sequence(items[7][1])]
    return SearchRequest(
        base=ber.decode_string(items[0][1]),
        scope=scope,
        deref_aliases=ber.decode_integer(items[2][1]),
        size_limit=max(0, ber.decode_integer(items[3][1])),
        time_limit=max(0, ber.decode_integer(items[4][1])),
        types_only=ber.decode_boolean(items[5][1]),
        filter=decode_filter(ftag, fcontent),
        attributes=attrs,
    )


def _parse_extended(content: bytes) -> ExtendedRequest:
    name = None
    value = None
    for tag, item in ber.decode_sequence(content):
        if tag == ber.context(0):
            name = ber.decode_string(item)
        elif tag == ber.context(1):
            value = item
    if name is None:
        raise ProtocolError("ExtendedRequest without requestName")
    return ExtendedRequest(name, value)


def parse_message(pdu: bytes) -> LDAPMessage:
    """Decode one complete LDAPMessage PDU. Raises :class:`ber.BERError` / :class:`ProtocolError`."""
    try:
        tag, content = ber.decode(pdu)
    except ber.BERError:
        raise
    if tag != ber.TAG_SEQUENCE:
        raise ProtocolError("LDAPMessage must be a SEQUENCE")
    items = ber.decode_sequence(content)
    if len(items) < 2:
        raise ProtocolError("LDAPMessage needs messageID and protocolOp")
    message_id = ber.decode_integer(items[0][1])
    op_tag, op_content = items[1]
    controls = items[2][1] if len(items) > 2 and items[2][0] == ber.context(0, True) else None
    if (op_tag & 0xC0) != ber.CLASS_APPLICATION:
        raise ProtocolError(f"protocolOp has non-application tag {op_tag:#x}")
    number = op_tag & 0x1F
    if number == BIND_REQUEST:
        op = _parse_bind(op_content)
    elif number == UNBIND_REQUEST:
        op = UnbindRequest()
    elif number == SEARCH_REQUEST:
        op = _parse_search(op_content)
    elif number == ABANDON_REQUEST:
        op = AbandonRequest(ber.decode_integer(op_content))
    elif number == EXTENDED_REQUEST:
        op = _parse_extended(op_content)
    else:
        op = UnsupportedRequest(number)
    return LDAPMessage(message_id, op, controls)


# --------------------------------------------------------------------------- responses

def _message(message_id: int, op: bytes) -> bytes:
    return ber.encode_sequence(ber.encode_integer(message_id), op)


def _ldap_result(code: int, matched_dn: str = "", message: str = "") -> bytes:
    return (ber.encode_enumerated(code) + ber.encode_octet_string(matched_dn)
            + ber.encode_octet_string(message))


def result_response(message_id: int, app_tag: int, code: int, matched_dn: str = "", message: str = "") -> bytes:
    """Generic ``LDAPResult`` response (used for BindResponse, SearchResultDone, Modify/Add/... responses)."""
    return _message(message_id, ber.encode_sequence(_ldap_result(code, matched_dn, message),
                                                    tag=ber.application(app_tag)))


def bind_response(message_id: int, code: int, message: str = "") -> bytes:
    return result_response(message_id, BIND_RESPONSE, code, "", message)


def search_result_done(message_id: int, code: int, matched_dn: str = "", message: str = "") -> bytes:
    return result_response(message_id, SEARCH_RESULT_DONE, code, matched_dn, message)


def search_result_entry(message_id: int, dn: str, attributes: dict[str, list[str]]) -> bytes:
    attrs = [
        ber.encode_sequence(ber.encode_octet_string(name),
                            ber.encode_set(*(ber.encode_octet_string(str(v)) for v in values)))
        for name, values in attributes.items()
    ]
    op = ber.encode_sequence(ber.encode_octet_string(dn), ber.encode_sequence(*attrs),
                             tag=ber.application(SEARCH_RESULT_ENTRY))
    return _message(message_id, op)


def extended_response(message_id: int, code: int, message: str = "", name: str | None = None,
                      value: bytes | None = None) -> bytes:
    body = _ldap_result(code, "", message)
    if name is not None:
        body += ber.encode_octet_string(name, tag=ber.context(10))
    if value is not None:
        body += ber.encode_octet_string(value, tag=ber.context(11))
    return _message(message_id, ber.encode_sequence(body, tag=ber.application(EXTENDED_RESPONSE)))


def notice_of_disconnection(code: int = PROTOCOL_ERROR, message: str = "") -> bytes:
    """Unsolicited notification (message id 0) sent right before the server closes the connection."""
    return extended_response(0, code, message, name=OID_NOTICE_OF_DISCONNECTION)


# --------------------------------------------------------------------------- client-side requests

def bind_request(message_id: int, dn: str = "", password: str | bytes = "", version: int = 3) -> bytes:
    if isinstance(password, str):
        password = password.encode("utf-8")
    op = ber.encode_sequence(ber.encode_integer(version), ber.encode_octet_string(dn),
                             ber.encode_octet_string(password, tag=ber.context(0)),
                             tag=ber.application(BIND_REQUEST))
    return _message(message_id, op)


def unbind_request(message_id: int) -> bytes:
    return _message(message_id, ber.encode_tlv(ber.application(UNBIND_REQUEST, constructed=False), b""))


def search_request(message_id: int, base: str, flt, scope: int = SCOPE_SUB, attributes=(),
                   size_limit: int = 0, time_limit: int = 0, types_only: bool = False) -> bytes:
    if isinstance(flt, str):
        flt = parse_filter(flt)
    op = ber.encode_sequence(
        ber.encode_octet_string(base), ber.encode_enumerated(scope), ber.encode_enumerated(0),
        ber.encode_integer(size_limit), ber.encode_integer(time_limit), ber.encode_boolean(types_only),
        encode_filter(flt), ber.encode_sequence(*(ber.encode_octet_string(a) for a in attributes)),
        tag=ber.application(SEARCH_REQUEST),
    )
    return _message(message_id, op)


def extended_request(message_id: int, name: str, value: bytes | None = None) -> bytes:
    body = ber.encode_octet_string(name, tag=ber.context(0))
    if value is not None:
        body += ber.encode_octet_string(value, tag=ber.context(1))
    return _message(message_id, ber.encode_sequence(body, tag=ber.application(EXTENDED_REQUEST)))


def abandon_request(message_id: int, target_id: int) -> bytes:
    return _message(message_id, ber.encode_integer(target_id, tag=ber.application(ABANDON_REQUEST, False)))


@dataclass
class Response:
    """Decoded server response (client side): ``op_tag`` is the application tag number."""

    message_id: int
    op_tag: int
    result_code: int | None = None
    matched_dn: str = ""
    message: str = ""
    dn: str = ""
    attributes: dict[str, list[str]] = field(default_factory=dict)
    name: str | None = None
    value: bytes | None = None


def parse_response(pdu: bytes) -> Response:
    tag, content = ber.decode(pdu)
    if tag != ber.TAG_SEQUENCE:
        raise ProtocolError("LDAPMessage must be a SEQUENCE")
    items = ber.decode_sequence(content)
    message_id = ber.decode_integer(items[0][1])
    op_tag, op_content = items[1]
    number = op_tag & 0x1F
    resp = Response(message_id, number)
    parts = ber.decode_sequence(op_content)
    if number == SEARCH_RESULT_ENTRY:
        resp.dn = ber.decode_string(parts[0][1])
        for _t, attr in ber.decode_sequence(parts[1][1]):
            name_item, vals_item = ber.decode_sequence(attr)
            resp.attributes[ber.decode_string(name_item[1])] = [
                ber.decode_string(v) for _vt, v in ber.decode_sequence(vals_item[1])]
        return resp
    resp.result_code = ber.decode_integer(parts[0][1])
    resp.matched_dn = ber.decode_string(parts[1][1])
    resp.message = ber.decode_string(parts[2][1])
    for t, v in parts[3:]:
        if t == ber.context(10):
            resp.name = ber.decode_string(v)
        elif t == ber.context(11):
            resp.value = v
    return resp
