"""The mail double: SMTP in, IMAP and IMAPS out, a runtime test CA and HTTP control routes.

One `MailDouble` runs four listeners on the same address: the control server (`url`, HTTP), SMTP, IMAP and, with TLS
(the default), IMAPS. Without an explicit `ca` it creates a `DoubleCA` for ``localhost``, ``127.0.0.1``, ``::1``,
the bound address and `ca_names`, in `ca_dir` (reusing a CA already there) or in a temporary directory removed by
`stop`.

Control routes (not journaled, not counted):

- ``GET /_double/state``: the common state document plus ``messages`` (counts per user and folder) and
  ``endpoints``; ``done`` is always true (the double has no expectations), ``ok`` means no failure.
- ``GET /_double/journal``: SMTP and IMAP commands (`agent_doubles.mail.record`).
- ``POST /_double/reset``: empties every folder and the journal in memory and clears the failures; accounts stay.
  IMAP clients with a selected folder must reconnect after a reset (no EXPUNGE responses are sent).
- ``GET|POST /_double/accounts``: list accounts; add one ``{"user", "password", "junk_name"?, "junk_attribute"?,
  "delimiter"?}`` (``201``).
- ``POST /_double/deliver``: store a message without SMTP, ``{"to", "from", "subject"?, "text"?, "html"?, "date"?,
  "spam"?, "folder"?}`` or ``{"to", "from", "raw", "folder"?}``; answers ``{"delivered": [{"user", "folder",
  "uid"}]}``.
- ``GET /_double/messages?user=…&folder=…``: stored messages (headers, flags, size).
- ``GET /_double/info``: the endpoints (also written to `info_path` at start); ``GET /_double/ca``: the CA in PEM.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
from collections.abc import Callable, Iterable
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, ClassVar, Self
from urllib.parse import parse_qsl, urlsplit

from agent_doubles.common.journal import Journal
from agent_doubles.common.server import BodyError, DoubleHandler, DoubleServer
from agent_doubles.common.state import state_document
from agent_doubles.mail.control import Action, ControlError, Raw, body_object, handle_control
from agent_doubles.mail.imap import IMAPConnection
from agent_doubles.mail.message import compose
from agent_doubles.mail.record import Recorder
from agent_doubles.mail.smtp import SMTPConnection
from agent_doubles.mail.store import MailStore, StoreError, parse_account
from agent_doubles.mail.tcp import Connection, LineServer
from agent_doubles.mail.testca import DEFAULT_NAMES, DoubleCA

DOUBLE_NAME = "mail"


def ca_name_list(host: str, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """The certificate names of a double bound to `host`: the loopback names, `host` and `extra`, without repeats."""
    names: list[str] = []
    for name in (*DEFAULT_NAMES, host, *extra):
        if name not in names:
            names.append(name)
    return tuple(names)


def _text(body: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise ControlError(f"{key} must be a string")
    return value


def _recipients(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ControlError("to must be an address or a non-empty list of addresses")
    return value


class MailDouble:
    """A running mail double (see the module documentation)."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        smtp_port: int = 0,
        imap_port: int = 0,
        imaps_port: int = 0,
        tls: bool = True,
        ca: DoubleCA | None = None,
        ca_dir: Path | None = None,
        journal_path: str | Path | None = None,
        info_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        junk_senders: Iterable[str] = (),
        ca_names: Iterable[str] = (),
        openssl: str = "openssl",
    ) -> None:
        self.clock = clock
        self.info_path = info_path
        self.store = MailStore(clock=clock, junk_senders=junk_senders)
        self.recorder = Recorder(Journal(DOUBLE_NAME, journal_path))
        self._own_ca_dir: Path | None = None
        self._servers: list[Any] = []
        try:
            if tls and ca is None:
                if ca_dir is None:
                    ca_dir = self._own_ca_dir = Path(tempfile.mkdtemp(prefix="mail-double-ca-"))
                ca = DoubleCA.open_or_create(ca_dir, ca_name_list(host, ca_names), openssl=openssl)
            self.ca = ca if tls else None
            # Kept, so the state stays complete after `stop` removed a temporary CA.
            self._ca_file = None if self.ca is None else str(self.ca.ca_file)
            self._spki = None if self.ca is None else self.ca.spki_sha256()
            self.smtp = self._listen(LineServer(self._factory(SMTPConnection), host, smtp_port))
            self.imap = self._listen(LineServer(self._factory(IMAPConnection), host, imap_port))
            self.imaps: LineServer | None = None
            if self.ca is not None:
                tls_context = self.ca.server_context()
                self.imaps = self._listen(LineServer(self._factory(IMAPConnection), host, imaps_port, tls=tls_context))
            self._http = self._listen(DoubleServer(_handler_for(self), host, port))
        except BaseException:
            self._close()
            raise

    def _listen(self, server: Any) -> Any:
        self._servers.append(server)
        return server

    def _factory(
        self, kind: type[SMTPConnection] | type[IMAPConnection]
    ) -> Callable[[socket.socket, LineServer], Connection]:
        def make(sock: socket.socket, server: LineServer) -> Connection:
            return kind(sock, server, store=self.store, recorder=self.recorder)

        return make

    def _close(self) -> None:
        for server in reversed(self._servers):
            server.stop()
        self._servers = []
        if self._own_ca_dir is not None:
            shutil.rmtree(self._own_ca_dir, ignore_errors=True)
            self._own_ca_dir = None

    # --- lifecycle --------------------------------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The base URL of the control routes."""
        return str(self._http.url)

    def start(self) -> Self:
        for server in self._servers:
            server.start()
        if self.info_path is not None:
            temporary = self.info_path.with_name(f".{self.info_path.name}.tmp")
            temporary.write_text(json.dumps(self.endpoints(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.info_path)
        return self

    def stop(self) -> None:
        self._close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- configuration and views ------------------------------------------------------------------------------

    def add_account(self, user: str, password: str, **options: Any) -> None:
        """Create an account (`agent_doubles.mail.store.MailStore.add_account`)."""
        self.store.add_account(user, password, **options)

    def endpoints(self) -> dict[str, Any]:
        """Where clients reach the double, the CA file and the server key pin."""

        def address(server: LineServer | None) -> dict[str, Any] | None:
            return None if server is None else {"host": server.host, "port": server.port}

        return {
            "control": self.url,
            "smtp": address(self.smtp),
            "imap": address(self.imap),
            "imaps": address(self.imaps),
            "ca_file": self._ca_file,
            "spki_sha256": self._spki,
        }

    def state(self) -> dict[str, Any]:
        return state_document(
            DOUBLE_NAME,
            done=True,
            failures=self.recorder.failures(),
            requests=len(self.recorder.journal),
            messages=self.store.counts(),
            endpoints=self.endpoints(),
        )

    def reset(self) -> None:
        self.store.clear_messages()
        self.recorder.clear()

    def deliver(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """A control-route delivery (see the module documentation)."""
        to = _recipients(body.get("to"))
        sender = _text(body, "from", required=True)
        assert sender is not None
        folder = _text(body, "folder")
        raw = _text(body, "raw")
        spam = body.get("spam", False)
        if not isinstance(spam, bool):
            raise ControlError("spam must be a boolean")
        if raw is not None:
            data = raw.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
        else:
            data = compose(
                sender,
                to,
                _text(body, "subject") or "",
                when=self.clock(),
                text=_text(body, "text"),
                html=_text(body, "html"),
                date=_text(body, "date"),
                spam=spam,
            )
        try:
            delivered = self.store.deliver(data, sender=parseaddr(sender)[1], recipients=to, folder=folder)
        except StoreError as exc:
            raise ControlError(str(exc)) from None
        return [{"user": user, "folder": name, "uid": uid} for user, name, uid in delivered]


def _handler_for(double: MailDouble) -> type[BaseHTTPRequestHandler]:
    return type("MailControlHandler", (_ControlHandler,), {"double": double})


class _ControlHandler(DoubleHandler):
    server_version = "mail-double/1"
    double: ClassVar[MailDouble]

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def _route(self) -> None:
        parts = urlsplit(self.path)
        try:
            raw = self.read_body()
            problem: BodyError | None = None
        except BodyError as exc:
            raw, problem = b"", exc
        double = self.double
        if not parts.path.startswith("/_double/"):
            double.recorder.record("HTTP", parts.path, None, result="404", status=404, note="not a control route")
            double.recorder.fail(f"http {self.command} {parts.path}: not a control route")
            self.send_json(404, {"error": "the mail double serves only control routes over HTTP"})
            return
        routes: dict[tuple[str, str], Action] = {
            ("GET", "/_double/state"): lambda b, q: (200, double.state()),
            ("GET", "/_double/journal"): lambda b, q: (200, double.recorder.journal.records()),
            ("POST", "/_double/reset"): self._reset,
            ("GET", "/_double/accounts"): self._accounts,
            ("POST", "/_double/accounts"): self._add_account,
            ("POST", "/_double/deliver"): lambda b, q: (200, {"delivered": double.deliver(body_object(b))}),
            ("GET", "/_double/messages"): lambda b, q: (200, double.store.summary(q.get("user"), q.get("folder"))),
            ("GET", "/_double/info"): lambda b, q: (200, double.endpoints()),
            ("GET", "/_double/ca"): self._ca,
        }
        handle_control(self, routes, parts.path, query=dict(parse_qsl(parts.query)), raw=raw, problem=problem)

    def _reset(self, body: Any, query: dict[str, str]) -> tuple[int, Any]:
        self.double.reset()
        return 200, self.double.state()

    def _accounts(self, body: Any, query: dict[str, str]) -> tuple[int, Any]:
        accounts = self.double.store.accounts()
        return 200, [{"user": a.user, "folders": list(a.folders), "junk": a.junk} for a in accounts]

    def _add_account(self, body: Any, query: dict[str, str]) -> tuple[int, Any]:
        try:
            user, password, options = parse_account(body)
            self.double.add_account(user, password, **options)
        except StoreError as exc:
            raise ControlError(str(exc)) from None
        return 201, {"user": user}

    def _ca(self, body: Any, query: dict[str, str]) -> tuple[int, Any]:
        ca = self.double.ca
        if ca is None:
            raise ControlError("the double runs without TLS", 404)
        return 200, Raw("application/x-pem-file", ca.ca_pem().encode("ascii"))
