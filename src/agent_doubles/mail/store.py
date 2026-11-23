"""The mailboxes of the mail double: accounts, folders and messages, shared by SMTP, IMAP and the control routes.

Every account has an ``INBOX`` and a spam folder. The spam folder is called ``Junk`` and carries the RFC 6154
``\\Junk`` attribute by default; an account can name it differently and leave the attribute out (for example
``INBOX.Spam`` with delimiter ``.``), which exercises the name fallback of clients that look for spam folders by
name. A delivered message lands in the spam folder when the caller names it, when the message carries
``X-Spam-Flag: YES`` (as a spam filter would add) or when its envelope sender is listed in the store's junk senders;
otherwise in ``INBOX``.

Messages are only ever appended: sequence numbers and UIDs are stable for the life of a folder. Users are compared
without case. All methods are thread-safe.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email import message_from_bytes
from email import policy as email_policy
from email.message import Message
from typing import Any

INBOX = "INBOX"
DEFAULT_JUNK = "Junk"
JUNK_ATTRIBUTE = "\\Junk"
SEEN = "\\Seen"
SPAM_HEADER = "X-Spam-Flag"


class StoreError(ValueError):
    """An invalid account, folder or message."""


@dataclass
class StoredMessage:
    """One message: its UID in the folder, raw bytes (CRLF line ends), arrival time, flags and envelope."""

    uid: int
    raw: bytes
    internal_date: float
    sender: str
    recipients: tuple[str, ...]
    flags: set[str] = field(default_factory=set)

    def headers(self) -> Message:
        """The parsed header block (the body is parsed too, but nothing depends on it)."""
        return message_from_bytes(self.raw, policy=email_policy.compat32)

    def summary(self, folder: str) -> dict[str, Any]:
        """The JSON form the control route lists."""
        parsed = self.headers()
        return {
            "folder": folder,
            "uid": self.uid,
            "from": parsed.get("From"),
            "to": parsed.get("To"),
            "subject": parsed.get("Subject"),
            "date": parsed.get("Date"),
            "internal_date": self.internal_date,
            "sender": self.sender,
            "flags": sorted(self.flags),
            "size": len(self.raw),
        }


@dataclass
class Folder:
    """One folder: name, LIST attributes, UID validity and the messages."""

    name: str
    attributes: tuple[str, ...]
    uidvalidity: int
    messages: list[StoredMessage] = field(default_factory=list)
    uidnext: int = 1

    def append(self, raw: bytes, when: float, sender: str, recipients: tuple[str, ...]) -> StoredMessage:
        message = StoredMessage(self.uidnext, raw, when, sender, recipients)
        self.uidnext += 1
        self.messages.append(message)
        return message


@dataclass
class Account:
    """One mailbox account: user, password, hierarchy delimiter and folders in LIST order."""

    user: str
    password: str = field(repr=False)
    delimiter: str = "/"
    folders: dict[str, Folder] = field(default_factory=dict)
    junk: str = DEFAULT_JUNK

    def folder(self, name: str) -> Folder | None:
        """The folder `name` (``INBOX`` in any case)."""
        if name.upper() == INBOX:
            return self.folders[INBOX]
        return self.folders.get(name)


def _clean(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(c in value for c in '\r\n"\\'):
        raise StoreError(f"invalid {what}")
    return value


ACCOUNT_OPTIONS = (("junk_name", str), ("junk_attribute", bool), ("delimiter", str))


def parse_account(data: Any) -> tuple[str, str, dict[str, Any]]:
    """User, password and options of an account given as JSON (``{"user", "password", "junk_name"?,
    "junk_attribute"?, "delimiter"?}``); StoreError for anything else."""
    if not isinstance(data, dict):
        raise StoreError("an account must be an object")
    unknown = set(data) - {"user", "password"} - {key for key, _ in ACCOUNT_OPTIONS}
    if unknown:
        raise StoreError(f"unknown account keys {sorted(unknown)}")
    user, password = data.get("user"), data.get("password")
    if not isinstance(user, str) or not isinstance(password, str):
        raise StoreError("user and password must be strings")
    options: dict[str, Any] = {}
    for key, kind in ACCOUNT_OPTIONS:
        if key in data:
            if not isinstance(data[key], kind):
                raise StoreError(f"{key} has the wrong type")
            options[key] = data[key]
    return user, password, options


class MailStore:
    """All accounts of one mail double."""

    def __init__(self, *, clock: Callable[[], float] = time.time, junk_senders: Iterable[str] = ()) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        self._uidvalidity = int(clock())
        self.junk_senders = {sender.lower() for sender in junk_senders}

    def add_account(
        self,
        user: str,
        password: str,
        *,
        junk_name: str = DEFAULT_JUNK,
        junk_attribute: bool = True,
        delimiter: str = "/",
    ) -> Account:
        """Create (or replace) the account `user` with an empty INBOX and spam folder."""
        _clean(user, "user")
        _clean(password, "password")
        _clean(junk_name, "spam folder name")
        if junk_name.upper() == INBOX:
            raise StoreError("the spam folder cannot be INBOX")
        if delimiter not in ("/", "."):
            raise StoreError("invalid delimiter")
        with self.lock:
            self._uidvalidity += 1
            inbox = Folder(INBOX, ("\\HasNoChildren",), self._uidvalidity)
            attributes = ("\\HasNoChildren", JUNK_ATTRIBUTE) if junk_attribute else ("\\HasNoChildren",)
            junk = Folder(junk_name, attributes, self._uidvalidity)
            account = Account(user, password, delimiter, {INBOX: inbox, junk_name: junk}, junk_name)
            self._accounts[user.lower()] = account
            return account

    def account(self, user: str) -> Account | None:
        """The account `user`, or None."""
        with self.lock:
            return self._accounts.get(user.lower())

    def authenticate(self, user: str, password: str) -> Account | None:
        """The account when `password` is right, else None."""
        account = self.account(user)
        if account is None or account.password != password:
            return None
        return account

    def accounts(self) -> list[Account]:
        with self.lock:
            return list(self._accounts.values())

    def deliver(
        self,
        raw: bytes,
        *,
        sender: str,
        recipients: Iterable[str],
        folder: str | None = None,
    ) -> list[tuple[str, str, int]]:
        """Store `raw` for every recipient; ``(user, folder, uid)`` per delivery. Unknown recipients raise."""
        targets = list(recipients)
        if not targets:
            raise StoreError("no recipient")
        spam = str(message_from_bytes(raw).get(SPAM_HEADER, "")).strip().upper() == "YES"
        spam = spam or sender.lower() in self.junk_senders
        delivered = []
        with self.lock:
            accounts = []
            for recipient in targets:
                account = self.account(recipient)
                if account is None:
                    raise StoreError(f"unknown recipient {recipient}")
                accounts.append(account)
            when = self.clock()
            for account in accounts:
                name = folder if folder is not None else (account.junk if spam else INBOX)
                target = account.folder(name)
                if target is None:
                    raise StoreError(f"unknown folder {name}")
                stored = target.append(raw, when, sender, tuple(targets))
                delivered.append((account.user, target.name, stored.uid))
        return delivered

    def clear_messages(self) -> None:
        """Empty every folder (UIDs continue, so a client never sees a reused UID).

        Sessions that have a folder selected must reconnect afterwards: their sequence numbers no longer hold and the
        double sends no EXPUNGE responses.
        """
        with self.lock:
            for account in self._accounts.values():
                for stored in account.folders.values():
                    stored.messages.clear()

    def summary(self, user: str | None = None, folder: str | None = None) -> list[dict[str, Any]]:
        """Every stored message (of `user` / `folder` when given) in the control-route form."""
        with self.lock:
            out = []
            for account in self._accounts.values():
                if user is not None and account.user.lower() != user.lower():
                    continue
                for stored in account.folders.values():
                    if folder is not None and stored.name != folder:
                        continue
                    out += [{"user": account.user, **m.summary(stored.name)} for m in stored.messages]
            return out

    def counts(self) -> dict[str, dict[str, int]]:
        """Message counts per user and folder."""
        with self.lock:
            return {a.user: {f.name: len(f.messages) for f in a.folders.values()} for a in self._accounts.values()}
