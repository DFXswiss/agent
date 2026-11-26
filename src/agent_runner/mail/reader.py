"""One look into a mailbox over IMAP, read only (ADR-099 sections 4 and 6; 05-runner.md section 12.2).

`find(server, query)` opens one TLS connection (implicit TLS on the mailbox's port, the device's trust store, 30 s per
connection and command), signs in, and for each folder of the query (`INBOX`; the spam folder found by its `\\Junk`
flag, else by its name, `agent_cli.account_login`) opens it with `EXAMINE` and searches with `UID SEARCH` (sender,
subject, arrival dates). Of the at most `MAX_SCAN` newest hits per folder it reads the headers (`BODY.PEEK[HEADER]`,
`INTERNALDATE`, `RFC822.SIZE`), keeps the mails that arrived within `[since, until]`, and answers the `limit` newest
of all folders, newest first. With `body` (or for one mail named by `uid`) it reads each answered mail up to
`MAX_BODY_BYTES` (`BODY.PEEK[]`, partial) and adds its text (the plain-text part, else the HTML reduced to text) and
its distinct `http(s)` links; a larger mail keeps its headers and gets `text_truncated`. Nothing is marked read,
moved, deleted or sent: every fetch is a peek of a read-only selection, and it always logs out.

Failures: `Refused` (the server refused the sign-in or a command), `TimeoutError`/`OSError` (the network). The
password is used for `LOGIN` only and never appears in a message of this module.
"""

from __future__ import annotations

import contextlib
import imaplib
import re
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email import policy as email_policy
from email.header import decode_header, make_header
from email.message import EmailMessage, MIMEPart
from html.parser import HTMLParser
from typing import Any

from agent_cli.account_login import _junk_folder

TIMEOUT_SECONDS = 30.0
MAX_SCAN = 200
MAX_BODY_BYTES = 1024 * 1024
MAX_TEXT = 12000
MAX_LINKS = 20
MAX_LINK = 2000
MAX_FROM, MAX_TO, MAX_SUBJECT = 512, 2048, 1024
INBOX, SPAM = "inbox", "spam"
_META = re.compile(r"UID (\d+)|INTERNALDATE \"([^\"]+)\"|RFC822\.SIZE (\d+)")
_LINK = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
_TRAILING = ".,;:!?"

ImapFactory = Callable[..., Any]


class Refused(Exception):
    """The server refused the sign-in (`login`) or a command (`command`)."""

    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class Query:
    folder: str = "all"
    sender: str | None = None
    subject: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 5
    body: bool = False
    uid: tuple[str, int] | None = None
    """One mail `(folder, uid)` of an earlier answer."""


@dataclass
class Message:
    folder: str
    uid: int
    sender: str
    to: str
    subject: str
    date: datetime | None
    size: int
    text: str | None = None
    links: list[str] | None = None
    text_truncated: bool = False

    @property
    def id(self) -> str:
        return f"{self.folder}:{self.uid}"


def _imap_date(value: datetime) -> str:
    return value.strftime("%d-%b-%Y")


def _criteria(query: Query) -> list[str]:
    if query.uid is not None:
        return ["UID", str(query.uid[1])]
    found: list[str] = []
    if query.sender:
        found += ["FROM", f'"{query.sender}"']
    if query.subject:
        found += ["SUBJECT", f'"{query.subject}"']
    if query.since is not None:
        found += ["SINCE", _imap_date(query.since - timedelta(days=1))]
    if query.until is not None:
        found += ["BEFORE", _imap_date(query.until + timedelta(days=2))]
    return found or ["ALL"]


def _header(value: object, limit: int) -> str:
    if value is None:
        return ""
    try:
        text = str(make_header(decode_header(str(value))))
    except (ValueError, LookupError):
        text = str(value)
    return " ".join(text.split())[:limit]


def _arrival(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        return datetime.strptime(text.strip(), "%d-%b-%Y %H:%M:%S %z").astimezone(UTC)
    except ValueError:
        return None


def _fetched(data: Iterable[object]) -> list[tuple[dict[str, str], bytes]]:
    """The `(meta, literal)` pairs of a FETCH answer."""
    found = []
    for item in data:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        head, literal = item
        meta: dict[str, str] = {}
        for uid, internal, size in _META.findall(head.decode("ascii", "replace")):
            if uid:
                meta["uid"] = uid
            if internal:
                meta["date"] = internal
            if size:
                meta["size"] = size
        if "uid" in meta and isinstance(literal, bytes):
            found.append((meta, literal))
    return found


def _ok(answer: tuple[str, Any]) -> Any:
    status, data = answer
    if status != "OK":
        raise Refused("command")
    return data


def _headers(conn: Any, folder: str, uids: list[int]) -> list[Message]:
    if not uids:
        return []
    data = _ok(conn.uid("FETCH", ",".join(map(str, uids)), "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER])"))
    found = []
    for meta, literal in _fetched(data):
        head = message_from_bytes(literal, policy=email_policy.compat32)
        found.append(
            Message(
                folder=folder,
                uid=int(meta["uid"]),
                sender=_header(head.get("From"), MAX_FROM),
                to=_header(head.get("To"), MAX_TO),
                subject=_header(head.get("Subject"), MAX_SUBJECT),
                date=_arrival(meta.get("date")),
                size=int(meta.get("size", "0")),
            )
        )
    return found


class _Text(HTMLParser):
    """The text and the `href` links of an HTML part."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")
        for name, value in attrs:
            if tag == "a" and name == "href" and value:
                self.links.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _content(part: MIMEPart) -> str:
    try:
        return str(part.get_content())
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


def _links(candidates: Iterable[str]) -> list[str]:
    found: list[str] = []
    for candidate in candidates:
        link = candidate.rstrip(_TRAILING)
        if not link.isascii() or _LINK.fullmatch(link) is None or len(link) > MAX_LINK or link in found:
            continue
        found.append(link)
        if len(found) == MAX_LINKS:
            break
    return found


def _read(raw: bytes) -> tuple[str, list[str]]:
    """Text and links of a mail."""
    mail = message_from_bytes(raw, policy=email_policy.default)
    assert isinstance(mail, EmailMessage)
    plain, html = mail.get_body(preferencelist=("plain",)), mail.get_body(preferencelist=("html",))
    hrefs: list[str] = []
    if plain is not None:
        text = _content(plain)
    elif html is not None:
        parser = _Text()
        parser.feed(_content(html))
        text, hrefs = "".join(parser.parts), parser.links
    else:
        text = ""
    if html is not None and plain is not None:
        parser = _Text()
        parser.feed(_content(html))
        hrefs = parser.links
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()
    return text, _links([*_LINK.findall(text), *hrefs])


def _bodies(conn: Any, messages: list[Message]) -> None:
    for message in messages:
        if message.size > MAX_BODY_BYTES:
            message.text, message.links, message.text_truncated = "", [], True
            continue
        data = _ok(conn.uid("FETCH", str(message.uid), f"(UID BODY.PEEK[]<0.{MAX_BODY_BYTES + 1}>)"))
        raw = next((literal for _meta, literal in _fetched(data)), b"")
        text, links = _read(raw)
        message.text, message.links = text[:MAX_TEXT], links
        message.text_truncated = len(text) > MAX_TEXT


def _folders(conn: Any, query: Query) -> list[tuple[str, str]]:
    wanted = query.uid[0] if query.uid is not None else query.folder
    found = []
    if wanted in (INBOX, "all"):
        found.append((INBOX, "INBOX"))
    if wanted in (SPAM, "all"):
        junk = _junk_folder(conn)
        if junk is not None:
            found.append((SPAM, junk))
    return found


def _within(message: Message, query: Query) -> bool:
    if query.uid is not None:
        return True
    if message.date is None:
        return query.since is None and query.until is None
    return (query.since is None or message.date >= query.since) and (query.until is None or message.date <= query.until)


def _look(conn: Any, query: Query) -> list[Message]:
    names = dict(_folders(conn, query))
    found: list[Message] = []
    for folder, name in names.items():
        _ok(conn.select(name, readonly=True))
        hits = sorted(int(uid) for uid in (_ok(conn.uid("SEARCH", *_criteria(query)))[0] or b"").split())
        found += [message for message in _headers(conn, folder, hits[-MAX_SCAN:]) if _within(message, query)]
    newest = datetime.min.replace(tzinfo=UTC)
    found.sort(key=lambda message: (message.date or newest, message.uid), reverse=True)
    answer = found[: query.limit]
    if query.body or query.uid is not None:
        for folder, name in names.items():
            mine = [message for message in answer if message.folder == folder]
            if mine:
                _ok(conn.select(name, readonly=True))
                _bodies(conn, mine)
    return answer


def find(server: Server, query: Query, *, imap_factory: ImapFactory = imaplib.IMAP4_SSL) -> list[Message]:
    """One look (module docstring)."""
    conn = imap_factory(server.host, server.port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_SECONDS)
    try:
        try:
            conn.login(server.user, server.password)
        except imaplib.IMAP4.error:
            raise Refused("login") from None
        try:
            return _look(conn, query)
        except imaplib.IMAP4.error:
            raise Refused("command") from None
    finally:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            conn.logout()
