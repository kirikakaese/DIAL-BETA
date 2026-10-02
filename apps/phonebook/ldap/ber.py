"""Minimal ASN.1 BER codec - just what LDAP v3 (RFC 4511) needs.

Encoders return ``bytes``; decoders work on ``bytes``/``memoryview`` and raise :class:`BERError` on malformed
input instead of ``IndexError``/``ValueError`` so the server can drop the connection cleanly.
Tags are single identifier octets (class | constructed | number, number < 31), which covers every LDAP PDU.
"""
from __future__ import annotations

# identifier octet bits
CLASS_UNIVERSAL = 0x00
CLASS_APPLICATION = 0x40
CLASS_CONTEXT = 0x80
CONSTRUCTED = 0x20

# universal tags
TAG_BOOLEAN = 0x01
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_ENUMERATED = 0x0A
TAG_SEQUENCE = CONSTRUCTED | 0x10  # 0x30
TAG_SET = CONSTRUCTED | 0x11  # 0x31


class BERError(ValueError):
    """Malformed or truncated BER data."""


def application(number: int, constructed: bool = True) -> int:
    return CLASS_APPLICATION | (CONSTRUCTED if constructed else 0) | number


def context(number: int, constructed: bool = False) -> int:
    return CLASS_CONTEXT | (CONSTRUCTED if constructed else 0) | number


# --------------------------------------------------------------------------- encoding

def encode_length(n: int) -> bytes:
    if n < 0:
        raise BERError("negative length")
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def encode_tlv(tag: int, content: bytes) -> bytes:
    if not 0 <= tag <= 0xFF or (tag & 0x1F) == 0x1F:
        raise BERError(f"unsupported tag {tag!r}")
    return bytes([tag]) + encode_length(len(content)) + content


def encode_integer(value: int, tag: int = TAG_INTEGER) -> bytes:
    if value == 0:
        return encode_tlv(tag, b"\x00")
    length = (value.bit_length() + 8) // 8  # +1 bit for the sign
    return encode_tlv(tag, value.to_bytes(length, "big", signed=True))


def encode_enumerated(value: int) -> bytes:
    return encode_integer(value, TAG_ENUMERATED)


def encode_boolean(value: bool, tag: int = TAG_BOOLEAN) -> bytes:
    return encode_tlv(tag, b"\xff" if value else b"\x00")


def encode_octet_string(value: bytes | str, tag: int = TAG_OCTET_STRING) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return encode_tlv(tag, bytes(value))


def encode_sequence(*items: bytes, tag: int = TAG_SEQUENCE) -> bytes:
    return encode_tlv(tag, b"".join(items))


def encode_set(*items: bytes) -> bytes:
    return encode_sequence(*items, tag=TAG_SET)


def encode_null() -> bytes:
    return encode_tlv(TAG_NULL, b"")


# --------------------------------------------------------------------------- decoding

def decode_header(data: bytes | memoryview, offset: int = 0) -> tuple[int, int, int]:
    """``(tag, content_length, content_offset)`` of the TLV starting at ``offset``.

    Raises :class:`BERError` when the *header* is truncated or uses the indefinite/multi-byte tag forms.
    The content itself may still be incomplete - callers check ``content_offset + content_length``.
    """
    n = len(data)
    if offset + 2 > n:
        raise BERError("truncated header")
    tag = data[offset]
    if (tag & 0x1F) == 0x1F:
        raise BERError("multi-byte tags are not supported")
    first = data[offset + 1]
    pos = offset + 2
    if first < 0x80:
        return tag, first, pos
    nbytes = first & 0x7F
    if nbytes == 0:
        raise BERError("indefinite length is not allowed in LDAP")
    if nbytes > 4:
        raise BERError("length too large")
    if pos + nbytes > n:
        raise BERError("truncated length")
    length = int.from_bytes(bytes(data[pos:pos + nbytes]), "big")
    return tag, length, pos + nbytes


def pdu_length(buffer: bytes | bytearray) -> int | None:
    """Total size of the first TLV in ``buffer`` or ``None`` while its header is still incomplete.

    Used for framing on a stream: a 2-byte peek is enough to know whether more header bytes are needed.
    """
    try:
        _tag, length, start = decode_header(buffer)
    except BERError:
        if len(buffer) >= 6:  # enough bytes for any accepted header -> it really is malformed
            raise
        return None
    return start + length


def decode_tlv(data: bytes | memoryview, offset: int = 0) -> tuple[int, bytes, int]:
    """``(tag, content, next_offset)`` for the TLV at ``offset``; raises on truncation."""
    tag, length, start = decode_header(data, offset)
    end = start + length
    if end > len(data):
        raise BERError("truncated content")
    return tag, bytes(data[start:end]), end


def decode_sequence(content: bytes) -> list[tuple[int, bytes]]:
    """Split the content of a constructed element into ``[(tag, content), ...]``."""
    items = []
    offset = 0
    while offset < len(content):
        tag, value, offset = decode_tlv(content, offset)
        items.append((tag, value))
    return items


def decode_integer(content: bytes) -> int:
    if not content:
        raise BERError("empty INTEGER")
    if len(content) > 8:
        raise BERError("INTEGER too large")
    return int.from_bytes(content, "big", signed=True)


def decode_boolean(content: bytes) -> bool:
    if len(content) != 1:
        raise BERError("BOOLEAN must be one octet")
    return content[0] != 0


def decode_string(content: bytes) -> str:
    """LDAPString: UTF-8; invalid bytes are replaced rather than rejected (phones are not always tidy)."""
    return content.decode("utf-8", errors="replace")


def decode(data: bytes) -> tuple[int, bytes]:
    """Decode exactly one TLV that must span the whole buffer."""
    tag, content, end = decode_tlv(data)
    if end != len(data):
        raise BERError("trailing bytes after element")
    return tag, content
