"""The IMAP side of the mail double: an IMAP4rev1 subset (RFC 3501) over plain TCP or implicit TLS (IMAPS).

Supported commands and states:

- any state: ``CAPABILITY``, ``NOOP``, ``LOGOUT``;
- not authenticated: ``LOGIN user password`` (``NO [AUTHENTICATIONFAILED]`` for wrong credentials);
- authenticated: ``LIST reference pattern`` (reference ``""``; patterns with ``*`` and ``%``; the spam folder carries
  ``\\Junk`` unless the account leaves it out), ``SELECT`` and ``EXAMINE`` (``[READ-ONLY]``);
- selected: ``SEARCH``, ``FETCH``, ``UID SEARCH``, ``UID FETCH`` and ``CLOSE``.

``SEARCH`` keys: ``ALL``, ``FROM``/``TO``/``SUBJECT`` (substring of the header, without case), ``SINCE``/``BEFORE``/
``ON`` (arrival date, UTC), ``SENTSINCE``/``SENTBEFORE``/``SENTON`` (``Date`` header), ``SEEN``/``UNSEEN``,
``UID set``, a sequence set, ``NOT key``, ``OR key key`` and parenthesised groups; keys are combined with AND.
``FETCH`` items: ``FLAGS``, ``UID``, ``INTERNALDATE``, ``RFC822.SIZE``, ``RFC822``, ``RFC822.HEADER``,
``RFC822.TEXT``, ``BODY[]``/``BODY[HEADER]``/``BODY[TEXT]`` and their ``BODY.PEEK`` forms with an optional partial
``<origin.count>``, and the macro ``FAST``. A non-peek body fetch in a read-write selection sets ``\\Seen``.

Client literals (``{n}``) are accepted up to `MAX_LITERAL` bytes. Responses that announce new messages (``* n
EXISTS``) precede the results of a command in the selected state. ``BAD`` answers (unknown commands, syntax errors,
commands in the wrong state) are failures of the double's state; ``NO`` answers are legitimate test cases.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from agent_doubles.mail.record import redacted
from agent_doubles.mail.store import INBOX, SEEN, Account, Folder, MailStore, StoredMessage
from agent_doubles.mail.tcp import Connection

if TYPE_CHECKING:
    from agent_doubles.mail.record import Recorder
    from agent_doubles.mail.tcp import LineServer

CAPABILITIES = "IMAP4rev1"
MAX_LITERAL = 64 * 1024
SYSTEM_FLAGS = "(\\Answered \\Flagged \\Deleted \\Seen \\Draft)"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LITERAL = re.compile(r"\{(\d+)\}$")
_TAG = re.compile(r"([A-Za-z0-9.]+) ")
_BODY = re.compile(r"(BODY(?:\.PEEK)?)\[(|HEADER|TEXT)\](?:<(\d+)\.(\d+)>)?", re.IGNORECASE)
_SET = re.compile(r"(?:\d+|\*)(?::(?:\d+|\*))?(?:,(?:\d+|\*)(?::(?:\d+|\*))?)*")
_ATOM_SPECIALS = frozenset('(){ %*"\\]')
Token = Any  # str or list of tokens


class Bad(Exception):
    """A command answered with ``BAD``."""


class Literal(str):
    """A string sent as a literal."""


class TooLarge(str):
    """The announcement of a literal above `MAX_LITERAL` (never read)."""


def tokenize(segments: list[str]) -> list[Token]:
    """Tokens of a command: atoms and strings (`str`) and parenthesised lists (`list`)."""
    stack: list[list[Token]] = [[]]
    for segment in segments:
        if isinstance(segment, Literal):
            stack[-1].append(str(segment))
            continue
        if isinstance(segment, TooLarge):
            raise Bad("literal too large")
        i = 0
        while i < len(segment):
            char = segment[i]
            if char == " ":
                i += 1
            elif char == "(":
                stack.append([])
                i += 1
            elif char == ")":
                if len(stack) == 1:
                    raise Bad("unbalanced parenthesis")
                done = stack.pop()
                stack[-1].append(done)
                i += 1
            elif char == '"':
                i = _quoted(segment, i + 1, stack[-1])
            else:
                end = i
                while end < len(segment) and segment[end] not in ' ()"':
                    end += 1
                stack[-1].append(segment[i:end])
                i = end
    if len(stack) != 1:
        raise Bad("unbalanced parenthesis")
    return stack[0]


def _quoted(text: str, i: int, out: list[Token]) -> int:
    value = []
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            value.append(text[i + 1])
            i += 2
        elif char == '"':
            out.append("".join(value))
            return i + 1
        else:
            value.append(char)
            i += 1
    raise Bad("unterminated quoted string")


def quote(name: str) -> str:
    """`name` as an atom when possible, else as a quoted string."""
    if name and not any(c in _ATOM_SPECIALS or ord(c) < 0x21 for c in name):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_set(text: Any, largest: int) -> set[int]:
    """The numbers of a sequence or UID set (``*`` is `largest`).

    A range is cut at `largest`, so a huge range costs nothing; single numbers stay as given (FETCH reports them as
    out of range).
    """
    if not isinstance(text, str) or not _SET.fullmatch(text):
        raise Bad("invalid sequence set")
    numbers: set[int] = set()
    for part in text.split(","):
        low, _, high = part.partition(":")
        first = largest if low == "*" else int(low)
        last = first if not high else (largest if high == "*" else int(high))
        if not high:
            numbers.add(first)
            continue
        low_end, high_end = min(first, last), max(first, last)
        numbers.update(range(low_end, min(high_end, max(largest, low_end)) + 1))
    return numbers


def parse_date(text: Any) -> date:
    """An IMAP date ``d-Mon-yyyy``."""
    if isinstance(text, str):
        parts = text.split("-")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].title() in MONTHS and parts[2].isdigit():
            try:
                return date(int(parts[2]), MONTHS.index(parts[1].title()) + 1, int(parts[0]))
            except ValueError:
                pass
    raise Bad(f"invalid date {text!r}")


def internal_date(when: float) -> str:
    moment = datetime.fromtimestamp(when, UTC)
    return f'"{moment.day:02d}-{MONTHS[moment.month - 1]}-{moment.year} {moment:%H:%M:%S} +0000"'


def arrival_date(message: StoredMessage) -> date:
    return datetime.fromtimestamp(message.internal_date, UTC).date()


def sent_date(message: StoredMessage) -> date | None:
    header = message.headers().get("Date")
    if header is None:
        return None
    try:
        return parsedate_to_datetime(str(header)).date()
    except (TypeError, ValueError):
        return None


@dataclass
class View:
    """The selected folder and how far the client knows it."""

    folder: Folder
    readonly: bool
    known: int


Predicate = Callable[[int, StoredMessage], bool]


class SearchParser:
    """SEARCH keys to a predicate over (sequence number, message)."""

    def __init__(self, tokens: list[Token], view: View) -> None:
        self.tokens = list(tokens)
        self.view = view

    def parse(self) -> Predicate:
        if not self.tokens:
            raise Bad("SEARCH without keys")
        keys = []
        while self.tokens:
            keys.append(self.key())
        return lambda n, m: all(key(n, m) for key in keys)

    def value(self) -> str:
        if not self.tokens or not isinstance(self.tokens[0], str):
            raise Bad("SEARCH key without value")
        return str(self.tokens.pop(0))

    def key(self) -> Predicate:
        token = self.tokens.pop(0)
        if isinstance(token, list):
            return SearchParser(token, self.view).parse()
        name = token.upper()
        build = getattr(self, f"key_{name.lower()}", None) if name.isalpha() else None
        if build is not None:
            predicate: Predicate = build(name)
            return predicate
        if _SET.fullmatch(token):
            numbers = parse_set(token, len(self.view.folder.messages))
            return lambda n, m: n in numbers
        raise Bad(f"unsupported SEARCH key {token}")

    def key_all(self, name: str) -> Predicate:
        return lambda n, m: True

    def key_from(self, name: str) -> Predicate:
        needle = self.value().casefold()
        return lambda n, m: needle in str(m.headers().get(name.title(), "")).casefold()

    key_to = key_subject = key_from

    def key_since(self, name: str) -> Predicate:
        return _date_key(name, parse_date(self.value()), arrival_date)

    key_before = key_on = key_since

    def key_sentsince(self, name: str) -> Predicate:
        return _date_key(name[4:], parse_date(self.value()), sent_date)

    key_sentbefore = key_senton = key_sentsince

    def key_seen(self, name: str) -> Predicate:
        return lambda n, m: SEEN in m.flags

    def key_unseen(self, name: str) -> Predicate:
        return lambda n, m: SEEN not in m.flags

    def key_not(self, name: str) -> Predicate:
        inner = self.key_after(name)
        return lambda n, m: not inner(n, m)

    def key_or(self, name: str) -> Predicate:
        left = self.key_after(name)
        right = self.key_after(name)
        return lambda n, m: left(n, m) or right(n, m)

    def key_uid(self, name: str) -> Predicate:
        uids = parse_set(self.value(), self.view.folder.uidnext - 1)
        return lambda n, m: m.uid in uids

    def key_after(self, name: str) -> Predicate:
        if not self.tokens:
            raise Bad(f"{name} without a key")
        return self.key()


def _date_key(kind: str, when: date, of: Callable[[StoredMessage], date | None]) -> Predicate:
    def check(n: int, m: StoredMessage) -> bool:
        day = of(m)
        if day is None:
            return False
        if kind == "SINCE":
            return day >= when
        if kind == "BEFORE":
            return day < when
        return day == when

    return check


@dataclass(frozen=True)
class Item:
    """One FETCH item: the name in the response, the section, the partial range and whether it sets \\Seen."""

    kind: str
    section: str = ""
    origin: int | None = None
    count: int = 0
    sets_seen: bool = False


def parse_items(token: Token) -> list[Item]:
    names: list[Token] = token if isinstance(token, list) else [token]
    if not names:
        raise Bad("empty FETCH item list")
    items = []
    for name in names:
        if not isinstance(name, str):
            raise Bad("invalid FETCH item")
        upper = name.upper()
        if upper == "FAST":
            items += [Item("FLAGS"), Item("INTERNALDATE"), Item("RFC822.SIZE")]
        elif upper in ("FLAGS", "UID", "INTERNALDATE", "RFC822.SIZE", "RFC822.HEADER"):
            items.append(Item(upper))
        elif upper in ("RFC822", "RFC822.TEXT"):
            items.append(Item(upper, sets_seen=True))
        else:
            found = _BODY.fullmatch(name)
            if found is None:
                raise Bad(f"unsupported FETCH item {name}")
            peek = found.group(1).upper() == "BODY.PEEK"
            origin = None if found.group(3) is None else int(found.group(3))
            count = 0 if found.group(4) is None else int(found.group(4))
            items.append(Item("BODY", found.group(2).upper(), origin, count, sets_seen=not peek))
    return items


def _split(raw: bytes) -> tuple[bytes, bytes]:
    end = raw.find(b"\r\n\r\n")
    if end == -1:
        return raw, b""
    return raw[: end + 4], raw[end + 4 :]


def _literal(data: bytes) -> bytes:
    return b"{%d}\r\n" % len(data) + data


class IMAPConnection(Connection):
    """One IMAP session."""

    def __init__(self, sock: socket.socket, server: LineServer, *, store: MailStore, recorder: Recorder) -> None:
        super().__init__(sock, server)
        self.store = store
        self.recorder = recorder
        self.protocol = "IMAP" if server.tls is None else "IMAPS"
        self.account: Account | None = None
        self.view: View | None = None
        self.closing = False

    # -- reading ---------------------------------------------------------------------------------------------------

    def read_command(self) -> list[str] | None:
        """The segments of one command (text and literals), or None at end of stream."""
        segments: list[str] = []
        while True:
            line = self.readline()
            if not line:
                return None
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            found = _LITERAL.search(text)
            if found is None:
                segments.append(text)
                return segments
            size = int(found.group(1))
            if size > MAX_LITERAL:
                segments.append(TooLarge(text))
                return segments
            segments.append(text[: found.start()])
            self.write(b"+ Ready for literal data\r\n")
            data = self.read_exact(size)
            if len(data) < size:
                return None
            segments.append(Literal(data.decode("utf-8", "replace")))

    # -- writing ---------------------------------------------------------------------------------------------------

    def untagged(self, text: str) -> None:
        self.write(f"* {text}\r\n".encode())

    def complete(self, tag: str, result: str, text: str, command: str, body: Any) -> None:
        """Record the command, then send the tagged reply (a client that read the reply finds the record)."""
        note = None if result == "OK" else text
        self.recorder.record(self.protocol, command, body, result=result, note=note)
        if result == "BAD":
            self.recorder.fail(f"imap {command}: {text}")
        self.write(f"{tag} {result} {text}\r\n".encode())

    # -- loop ------------------------------------------------------------------------------------------------------

    def serve(self) -> None:
        self.untagged(f"OK [CAPABILITY {CAPABILITIES}] mail double ready")
        while not self.closing:
            segments = self.read_command()
            if segments is None:
                return
            guess = _TAG.match(segments[0])
            tag, command = (guess.group(1) if guess else "*"), "(invalid)"
            body: Any = None
            try:
                tokens = tokenize(segments)
                if len(tokens) < 2 or not all(isinstance(t, str) and t for t in tokens[:2]):
                    raise Bad("missing tag or command")
                tag, command = tokens[0], tokens[1].upper()
                args = tokens[2:]
                body = "<redacted>" if command == "LOGIN" else args
                handler = getattr(self, f"cmd_{command.lower()}", None) if command.isalpha() else None
                if handler is None:
                    raise Bad("unknown command")
                body = handler(tag, args)
            except Bad as exc:
                self.complete(tag, "BAD", str(exc), command, body)

    def need(self, state: str) -> None:
        if state == "unauthenticated" and self.account is not None:
            raise Bad("already authenticated")
        if state == "authenticated" and self.account is None:
            raise Bad("not authenticated")

    def selected(self) -> View:
        if self.view is None:
            raise Bad("no mailbox selected")
        count = len(self.view.folder.messages)
        if count != self.view.known:
            self.untagged(f"{count} EXISTS")
            self.view.known = count
        return self.view

    @staticmethod
    def args(args: list[Token], count: int) -> list[str]:
        if len(args) != count or not all(isinstance(a, str) for a in args):
            raise Bad("invalid arguments")
        return [str(a) for a in args]

    # -- any state -------------------------------------------------------------------------------------------------

    def cmd_capability(self, tag: str, args: list[Token]) -> Any:
        self.untagged(f"CAPABILITY {CAPABILITIES}")
        self.complete(tag, "OK", "CAPABILITY completed", "CAPABILITY", args)
        return args

    def cmd_noop(self, tag: str, args: list[Token]) -> Any:
        if self.view is not None:
            self.selected()
        self.complete(tag, "OK", "NOOP completed", "NOOP", args)
        return args

    def cmd_logout(self, tag: str, args: list[Token]) -> Any:
        self.untagged("BYE mail double logging out")
        self.complete(tag, "OK", "LOGOUT completed", "LOGOUT", args)
        self.closing = True
        return args

    # -- not authenticated -----------------------------------------------------------------------------------------

    def cmd_login(self, tag: str, args: list[Token]) -> Any:
        self.need("unauthenticated")
        user, password = self.args(args, 2)
        body = {"user": user, "password": redacted(password)}
        account = self.store.authenticate(user, password)
        if account is None:
            self.complete(tag, "NO", "[AUTHENTICATIONFAILED] Invalid credentials", "LOGIN", body)
            return body
        self.account = account
        self.complete(tag, "OK", f"[CAPABILITY {CAPABILITIES}] LOGIN completed", "LOGIN", body)
        return body

    # -- authenticated ---------------------------------------------------------------------------------------------

    def cmd_list(self, tag: str, args: list[Token]) -> Any:
        self.need("authenticated")
        reference, pattern = self.args(args, 2)
        assert self.account is not None
        if reference == "":
            delimiter = self.account.delimiter
            matcher = _list_pattern(pattern, delimiter)
            for folder in self.account.folders.values():
                if matcher.fullmatch(folder.name):
                    attributes = " ".join(folder.attributes)
                    self.untagged(f'LIST ({attributes}) "{delimiter}" {quote(folder.name)}')
        self.complete(tag, "OK", "LIST completed", "LIST", args)
        return args

    def cmd_select(self, tag: str, args: list[Token]) -> Any:
        return self._select(tag, args, readonly=False, command="SELECT")

    def cmd_examine(self, tag: str, args: list[Token]) -> Any:
        return self._select(tag, args, readonly=True, command="EXAMINE")

    def _select(self, tag: str, args: list[Token], *, readonly: bool, command: str) -> Any:
        self.need("authenticated")
        (name,) = self.args(args, 1)
        assert self.account is not None
        self.view = None
        folder = self.account.folder(name)
        if folder is None:
            self.complete(tag, "NO", "[NONEXISTENT] Mailbox does not exist", command, args)
            return args
        with self.store.lock:
            count = len(folder.messages)
            uidnext = folder.uidnext
        self.untagged(f"FLAGS {SYSTEM_FLAGS}")
        self.untagged(f"{count} EXISTS")
        self.untagged("0 RECENT")
        self.untagged(f"OK [UIDVALIDITY {folder.uidvalidity}] UIDs valid")
        self.untagged(f"OK [UIDNEXT {uidnext}] Predicted next UID")
        self.view = View(folder, readonly, count)
        mode = "[READ-ONLY]" if readonly else "[READ-WRITE]"
        self.complete(tag, "OK", f"{mode} {command} completed", command, args)
        return args

    # -- selected --------------------------------------------------------------------------------------------------

    def cmd_close(self, tag: str, args: list[Token]) -> Any:
        self.need("authenticated")
        self.selected()
        self.view = None
        self.complete(tag, "OK", "CLOSE completed", "CLOSE", args)
        return args

    def cmd_search(self, tag: str, args: list[Token], *, uid: bool = False) -> Any:
        self.need("authenticated")
        view = self.selected()
        command = "UID SEARCH" if uid else "SEARCH"
        predicate = SearchParser(args, view).parse()
        with self.store.lock:
            messages = list(view.folder.messages[: view.known])
        hits = [str(m.uid if uid else n) for n, m in enumerate(messages, 1) if predicate(n, m)]
        self.untagged(" ".join(["SEARCH", *hits]))
        self.complete(tag, "OK", f"{command} completed", command, args)
        return args

    def cmd_fetch(self, tag: str, args: list[Token], *, uid: bool = False) -> Any:
        self.need("authenticated")
        view = self.selected()
        command = "UID FETCH" if uid else "FETCH"
        if len(args) != 2:
            raise Bad("invalid arguments")
        items = parse_items(args[1])
        if uid and not any(item.kind == "UID" for item in items):
            items.insert(0, Item("UID"))
        with self.store.lock:
            messages = list(view.folder.messages[: view.known])
        if uid:
            wanted = parse_set(args[0], view.folder.uidnext - 1)
            chosen = [(n, m) for n, m in enumerate(messages, 1) if m.uid in wanted]
        else:
            numbers = parse_set(args[0], len(messages))
            if numbers and (min(numbers) < 1 or max(numbers) > len(messages)):
                raise Bad("message number out of range")
            chosen = [(n, messages[n - 1]) for n in sorted(numbers)]
        for number, message in chosen:
            self.write(b"* %d FETCH (" % number + self.fetch_items(message, items, view) + b")\r\n")
        self.complete(tag, "OK", f"{command} completed", command, args)
        return args

    def fetch_items(self, message: StoredMessage, items: list[Item], view: View) -> bytes:
        parts: list[bytes] = []
        newly_seen = False
        for item in items:
            if item.sets_seen and not view.readonly and SEEN not in message.flags:
                with self.store.lock:
                    message.flags.add(SEEN)
                newly_seen = True
            parts.append(self.fetch_item(message, item))
        if newly_seen and not any(item.kind == "FLAGS" for item in items):
            parts.append(self.fetch_item(message, Item("FLAGS")))
        return b" ".join(parts)

    @staticmethod
    def fetch_item(message: StoredMessage, item: Item) -> bytes:
        header, text = _split(message.raw)
        if item.kind == "FLAGS":
            return f"FLAGS ({' '.join(sorted(message.flags))})".encode()
        if item.kind == "UID":
            return b"UID %d" % message.uid
        if item.kind == "INTERNALDATE":
            return f"INTERNALDATE {internal_date(message.internal_date)}".encode()
        if item.kind == "RFC822.SIZE":
            return b"RFC822.SIZE %d" % len(message.raw)
        if item.kind.startswith("RFC822"):
            data = {"RFC822": message.raw, "RFC822.HEADER": header, "RFC822.TEXT": text}[item.kind]
            return item.kind.encode() + b" " + _literal(data)
        data = {"": message.raw, "HEADER": header, "TEXT": text}[item.section]
        label = f"BODY[{item.section}]"
        if item.origin is not None:
            data = data[item.origin : item.origin + item.count]
            label += f"<{item.origin}>"
        return label.encode() + b" " + _literal(data)

    def cmd_uid(self, tag: str, args: list[Token]) -> Any:
        if not args or not isinstance(args[0], str) or args[0].upper() not in ("SEARCH", "FETCH"):
            raise Bad("unsupported UID command")
        if args[0].upper() == "SEARCH":
            return self.cmd_search(tag, args[1:], uid=True)
        return self.cmd_fetch(tag, args[1:], uid=True)


def _list_pattern(pattern: str, delimiter: str) -> re.Pattern[str]:
    parts = []
    for char in pattern:
        if char == "*":
            parts.append(".*")
        elif char == "%":
            parts.append(f"[^{re.escape(delimiter)}]*")
        else:
            parts.append(re.escape(char))
    regex = "".join(parts)
    # INBOX matches without case (RFC 3501 section 5.1).
    if pattern.upper() == INBOX:
        return re.compile(regex, re.IGNORECASE)
    return re.compile(regex)
