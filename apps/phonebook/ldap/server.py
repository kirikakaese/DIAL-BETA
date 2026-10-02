"""asyncio LDAP v3 server serving every event's phonebook read-only on one port.

Authentication: ``cn=directory,dc=<slug>,dc=pet`` + the event's ``PhonebookSettings.directory_token`` binds a
connection to that event; anonymous binds (and unbound searches) are honoured only with
``PET_LDAP_ALLOW_ANONYMOUS``. The root DSE is always readable but lists ``namingContexts`` only for what the
connection may search. Write operations answer ``unwillingToPerform``; malformed PDUs end the connection.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import signal

from asgiref.sync import sync_to_async
from django.conf import settings as dj_settings

from . import ber, protocol
from .directory import DirectoryCache, Entry, EventDirectory, bind_dn, bind_dn_slug, event_dn, event_slug_from_dn

logger = logging.getLogger("pet.ldap")

READ_CHUNK = 64 * 1024
IDLE_TIMEOUT = 15 * 60  # seconds without a PDU before the connection is dropped (like slapd idletimeout)


class _Connection:
    def __init__(self, server: LDAPServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.bound_slug: str | None = None
        self.closed = False

    # ------------------------------------------------------------------ plumbing

    async def send(self, pdu: bytes) -> None:
        self.writer.write(pdu)
        await self.writer.drain()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass

    async def run(self) -> None:
        buf = bytearray()
        try:
            while not self.closed:
                need = ber.pdu_length(buf)
                if need is not None and need > protocol.MAX_PDU_SIZE:
                    raise protocol.ProtocolError(f"PDU of {need} bytes exceeds the {protocol.MAX_PDU_SIZE} limit")
                if need is None or len(buf) < need:
                    chunk = await asyncio.wait_for(self.reader.read(READ_CHUNK), IDLE_TIMEOUT)
                    if not chunk:
                        return
                    buf += chunk
                    continue
                pdu = bytes(buf[:need])
                del buf[:need]
                message = protocol.parse_message(pdu)
                await self.dispatch(message)
        except (ber.BERError, protocol.ProtocolError) as exc:
            logger.warning("ldap %s: bad PDU (%s) - closing", self.peer, exc)
            try:
                await self.send(protocol.notice_of_disconnection(protocol.PROTOCOL_ERROR, str(exc)))
            except OSError:
                pass
        except TimeoutError:
            logger.info("ldap %s: idle timeout", self.peer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self.close()

    # ------------------------------------------------------------------ dispatch

    async def dispatch(self, message: protocol.LDAPMessage) -> None:
        op = message.op
        mid = message.message_id
        if isinstance(op, protocol.BindRequest):
            await self.bind(mid, op)
        elif isinstance(op, protocol.UnbindRequest):
            logger.debug("ldap %s: unbind", self.peer)
            await self.close()
        elif isinstance(op, protocol.SearchRequest):
            await self.search(mid, op)
        elif isinstance(op, protocol.AbandonRequest):
            pass  # searches are answered synchronously per connection - nothing left to abandon
        elif isinstance(op, protocol.ExtendedRequest):
            await self.extended(mid, op)
        elif isinstance(op, protocol.UnsupportedRequest) and op.tag in protocol.WRITE_OPERATIONS:
            await self.send(protocol.result_response(mid, protocol.WRITE_OPERATIONS[op.tag],
                                                     protocol.UNWILLING_TO_PERFORM, "",
                                                     "PET phonebook is read-only"))
        else:
            raise protocol.ProtocolError(f"unsupported operation {getattr(op, 'tag', '?')}")

    # ------------------------------------------------------------------ bind

    async def bind(self, mid: int, req: protocol.BindRequest) -> None:
        self.bound_slug = None  # RFC 4511 §4.2.1: a new bind resets the previous authorization
        if req.password is None:
            await self.send(protocol.bind_response(mid, protocol.AUTH_METHOD_NOT_SUPPORTED,
                                                   "only simple bind is supported"))
            return
        if req.version != 3:
            await self.send(protocol.bind_response(mid, protocol.PROTOCOL_ERROR, "LDAPv3 required"))
            return
        name = req.name.strip()
        if not name and not req.password:
            if self.server.allow_anonymous:
                logger.debug("ldap %s: anonymous bind", self.peer)
                await self.send(protocol.bind_response(mid, protocol.SUCCESS))
            else:
                logger.info("ldap %s: anonymous bind refused", self.peer)
                await self.send(protocol.bind_response(mid, protocol.INAPPROPRIATE_AUTHENTICATION,
                                                       "anonymous bind is disabled"))
            return
        if name and not req.password:
            await self.send(protocol.bind_response(mid, protocol.UNWILLING_TO_PERFORM,
                                                   "unauthenticated bind (DN without password) is not allowed"))
            return
        slug = bind_dn_slug(name)
        directory = await self.server.directory(slug) if slug else None
        ok = (directory is not None and directory.enabled
              and hmac.compare_digest(req.password, directory.token.encode("utf-8")))
        if not ok:
            logger.warning("ldap %s: invalid credentials for %r", self.peer, name)
            await asyncio.sleep(self.server.bind_failure_delay)
            await self.send(protocol.bind_response(mid, protocol.INVALID_CREDENTIALS, "invalid credentials"))
            return
        self.bound_slug = slug
        logger.info("ldap %s: bound to event %s", self.peer, slug)
        await self.send(protocol.bind_response(mid, protocol.SUCCESS))

    def may_search(self, slug: str) -> bool:
        if self.bound_slug is not None:
            return self.bound_slug == slug
        return self.server.allow_anonymous

    # ------------------------------------------------------------------ search

    async def root_dse(self) -> Entry:
        if self.bound_slug is not None:
            contexts = [event_dn(self.bound_slug)]
        elif self.server.allow_anonymous:
            contexts = await self.server.naming_contexts()
        else:
            contexts = []
        return Entry("", {
            "objectClass": ["top", "extensibleObject"],
            "namingContexts": contexts,
            "supportedLDAPVersion": ["3"],
            "supportedExtension": [protocol.OID_WHOAMI],
            "vendorName": ["PET"],
            "vendorVersion": ["PET Portable Event Telephone phonebook (read-only)"],
        })

    async def search(self, mid: int, req: protocol.SearchRequest) -> None:
        base = req.base.strip()
        if base == "" and req.scope == protocol.SCOPE_BASE:
            entry = await self.root_dse()
            if req.filter(entry.attributes):
                await self.send(protocol.search_result_entry(mid, "", entry.select(req.attributes, req.types_only)))
            await self.send(protocol.search_result_done(mid, protocol.SUCCESS))
            return

        if base == "":
            # one/sub level from the root: everything this connection may see
            if self.bound_slug is not None:
                slugs = [self.bound_slug]
            elif self.server.allow_anonymous:
                slugs = [event_slug_from_dn(dn) for dn in await self.server.naming_contexts()]
            else:
                await self.send(protocol.search_result_done(mid, protocol.INSUFFICIENT_ACCESS_RIGHTS, "",
                                                            "bind with cn=directory,dc=<event>,dc=pet first"))
                return
            matches: list[Entry] = []
            for slug in slugs:
                directory = await self.server.directory(slug)
                if directory is not None and directory.enabled:
                    found, _matched = directory.search(directory.base_dn, req.scope, req.filter)
                    matches += found or []
            await self.send_results(mid, req, matches)
            return

        slug = event_slug_from_dn(base)
        if slug is None:
            await self.send(protocol.search_result_done(mid, protocol.NO_SUCH_OBJECT, "",
                                                        "base must be below dc=<event>,dc=pet"))
            return
        if not self.may_search(slug):
            logger.info("ldap %s: search %r refused (bound to %r)", self.peer, base, self.bound_slug)
            await self.send(protocol.search_result_done(mid, protocol.INSUFFICIENT_ACCESS_RIGHTS, "",
                                                        "not authorised for this event"))
            return
        directory = await self.server.directory(slug)
        if directory is None or not directory.enabled:
            await self.send(protocol.search_result_done(mid, protocol.NO_SUCH_OBJECT, "",
                                                        "unknown event or directory disabled"))
            return
        matches, matched_dn = directory.search(base, req.scope, req.filter)
        if matches is None:
            await self.send(protocol.search_result_done(mid, protocol.NO_SUCH_OBJECT, matched_dn))
            return
        await self.send_results(mid, req, matches)

    async def send_results(self, mid: int, req: protocol.SearchRequest, matches: list[Entry]) -> None:
        limit = req.size_limit or len(matches)
        for entry in matches[:limit]:
            await self.send(protocol.search_result_entry(mid, entry.dn, entry.select(req.attributes, req.types_only)))
        if len(matches) > limit:
            logger.debug("ldap %s: %d of %d entries sent (sizeLimit)", self.peer, limit, len(matches))
            await self.send(protocol.search_result_done(mid, protocol.SIZE_LIMIT_EXCEEDED))
        else:
            logger.debug("ldap %s: search %r scope=%d -> %d entries", self.peer, req.base, req.scope, len(matches))
            await self.send(protocol.search_result_done(mid, protocol.SUCCESS))

    # ------------------------------------------------------------------ extended

    async def extended(self, mid: int, req: protocol.ExtendedRequest) -> None:
        if req.name == protocol.OID_WHOAMI:
            authz = f"dn:{bind_dn(self.bound_slug)}" if self.bound_slug else ""
            await self.send(protocol.extended_response(mid, protocol.SUCCESS, value=authz.encode("utf-8")))
        elif req.name == protocol.OID_START_TLS:
            await self.send(protocol.extended_response(mid, protocol.PROTOCOL_ERROR,
                                                       "StartTLS is not supported - use LDAPS (pet_ldap --cert)",
                                                       name=protocol.OID_START_TLS))
        else:
            await self.send(protocol.extended_response(mid, protocol.PROTOCOL_ERROR,
                                                       f"unsupported extended operation {req.name}"))


class LDAPServer:
    """``await start()`` binds the socket (``port`` may be 0 for an ephemeral one), ``stop()`` closes everything."""

    def __init__(self, host: str = "0.0.0.0", port: int = 3890, *, ssl_context=None,
                 allow_anonymous: bool | None = None, cache: DirectoryCache | None = None,
                 bind_failure_delay: float = 0.3):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.allow_anonymous = (bool(getattr(dj_settings, "PET_LDAP_ALLOW_ANONYMOUS", False))
                                if allow_anonymous is None else allow_anonymous)
        self.cache = cache or DirectoryCache()
        self.bind_failure_delay = bind_failure_delay
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[_Connection] = set()

    async def directory(self, slug: str) -> EventDirectory | None:
        return await sync_to_async(self.cache.get_event)(slug)

    async def naming_contexts(self) -> list[str]:
        return await sync_to_async(self.cache.naming_contexts)()

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = _Connection(self, reader, writer)
        self._connections.add(conn)
        logger.debug("ldap %s: connected", conn.peer)
        try:
            await conn.run()
        except Exception:  # noqa: BLE001 - never let one client take the server down
            logger.exception("ldap %s: unhandled error", conn.peer)
            await conn.close()
        finally:
            self._connections.discard(conn)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connection, self.host, self.port, ssl=self.ssl_context)
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("ldap%s: listening on %s:%d (anonymous %s)", "s" if self.ssl_context else "", self.host,
                    self.port, "allowed" if self.allow_anonymous else "disabled")

    async def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for conn in list(self._connections):
            await conn.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("ldap: stopped")


async def serve(host: str, port: int, ssl_context=None) -> None:
    """Run until SIGTERM/SIGINT (used by ``manage.py pet_ldap``)."""
    server = LDAPServer(host, port, ssl_context=ssl_context)
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            pass
    try:
        await stop.wait()
    finally:
        await server.stop()
