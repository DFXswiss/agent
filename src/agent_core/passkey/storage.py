"""The passkey tables of PASSKEY-PROTOCOL.md section 12 and every read and write the hub does on them.

The tables live in the hub's SQLite store (`AGENT_CORE_DATABASE`) and are created at start with the
other tables of that store (`Store.__init__`). Every binary value is stored as base64url text and
every time as integer seconds; no table holds a token, a private key or a secret.

The steps that a crash must not separate are each one write transaction (`Store.transaction`,
`BEGIN IMMEDIATE`): I2 with the insert of an invite, the bootstrap checks 3 and 4 with their insert,
O4 with the insert of the registration, R14 with the proposed id and the received key, B2 and B3
with the insert of the pending sign-in, S7 with the counter and the insert of the session, and C6.
R2 and S2 are committed on their own, before the later checks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..signing import Clock, SessionKey, b64url_decode, b64url_encode
from .encoding import CEREMONY_SECONDS, INVITE_OPEN_LIMIT, PENDING_SIGN_IN_LIMIT
from .webauthn import check_counter

if TYPE_CHECKING:
    from ..db import Store
    from ..keys import KeyRing

PASSKEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS passkey_invite (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  handle TEXT NOT NULL,
  team TEXT NOT NULL,
  created INTEGER NOT NULL,
  expires INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  bootstrap INTEGER NOT NULL CHECK (bootstrap IN (0, 1)),
  creation_request TEXT,
  opened INTEGER,
  received INTEGER,
  credential_id TEXT,
  public_key TEXT,
  proposed_id TEXT
);

CREATE TABLE IF NOT EXISTS passkey_registration (
  id TEXT PRIMARY KEY,
  invite_id TEXT NOT NULL UNIQUE REFERENCES passkey_invite(id),
  challenge TEXT NOT NULL,
  user_id TEXT NOT NULL,
  created INTEGER NOT NULL,
  finished INTEGER
);

CREATE TABLE IF NOT EXISTS passkey_sign_in (
  id TEXT PRIMARY KEY,
  statement TEXT NOT NULL,
  session_public_key TEXT NOT NULL UNIQUE,
  created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS passkey_session (
  id TEXT PRIMARY KEY,
  passkey_id TEXT NOT NULL,
  handle TEXT NOT NULL,
  credential_id TEXT NOT NULL,
  passkey_public_key TEXT NOT NULL,
  session_public_key TEXT NOT NULL UNIQUE,
  statement TEXT NOT NULL,
  authenticator_data TEXT NOT NULL,
  client_data_json TEXT NOT NULL,
  signature TEXT NOT NULL,
  created INTEGER NOT NULL,
  expires INTEGER NOT NULL,
  ended INTEGER
);

CREATE TABLE IF NOT EXISTS passkey_counter (
  passkey_id TEXT PRIMARY KEY,
  sign_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS command_sequence (
  sender TEXT PRIMARY KEY,
  last_sequence INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS passkey_sign_in_created_idx ON passkey_sign_in (created);
"""

# The statuses of section 5.2.
STATUS_PENDING = "pending"
STATUS_OPENED = "opened"
STATUS_RECEIVED = "received"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

# The outcomes of creating an invite (I2, bootstrap checks 3 and 4).
CREATED = "created"
BOOTSTRAP_OPEN = "bootstrap_open"
INVITE_LIMIT = "invite_limit"

# The refusals of opening an invite (O2 to O4) and of a begin (B2, B3).
INVITE_UNKNOWN = "invite_unknown"
INVITE_EXPIRED = "invite_expired"
INVITE_USED = "invite_used"
SESSION_KEY_KNOWN = "session_key_known"
TOO_MANY_PENDING = "too_many_pending"

# An invite is open (section 5.2: `pending` or `opened`) at the time bound to the first and second
# placeholder, with `CEREMONY_SECONDS` bound to the third. `i` is the invite and `r` its
# registration, left-joined by `_INVITES`.
_OPEN = (
    "i.received IS NULL AND r.finished IS NULL AND i.expires > ? "
    "AND (i.opened IS NULL OR ? - i.opened <= ?)"
)
_INVITE_COLUMNS = (
    "i.id, i.handle, i.team, i.created, i.expires, i.created_by, i.bootstrap, i.opened, "
    "i.received, i.credential_id, i.public_key, i.proposed_id, r.finished"
)
_INVITES = "FROM passkey_invite i LEFT JOIN passkey_registration r ON r.invite_id = i.id"


@dataclass(frozen=True)
class Invite:
    """An invite row with the `finished` time of its registration."""

    id: str
    handle: str
    team: str
    created: int
    expires: int
    created_by: str
    bootstrap: bool
    opened: int | None
    received: int | None
    credential_id: str | None
    public_key: str | None
    proposed_id: str | None
    finished: int | None

    def status(self, now: int) -> str:
        """Exactly one status of section 5.2, evaluated at `now`."""
        if self.received is not None:
            return STATUS_RECEIVED
        if self.finished is not None:
            return STATUS_FAILED
        if now >= self.expires:
            return STATUS_EXPIRED
        if self.opened is None:
            return STATUS_PENDING
        if now - self.opened <= CEREMONY_SECONDS:
            return STATUS_OPENED
        return STATUS_EXPIRED


@dataclass(frozen=True)
class NewInvite:
    """What the hub stores for a new invite (section 5.1). `token_hash` is the base64url of the
    SHA-256 of the token; the token itself is never stored."""

    id: str
    token_hash: str
    handle: str
    team: str
    created: int
    expires: int
    created_by: str
    bootstrap: bool
    creation_request: str | None


@dataclass(frozen=True)
class Registration:
    """A registration consumed at R2, with what R3 and R14 read of its invite."""

    id: str
    invite_id: str
    challenge: bytes
    opened: int
    expires: int
    handle: str
    bootstrap: bool


@dataclass(frozen=True)
class PendingSignIn:
    """A pending sign-in consumed at S2: its statement bytes and its begin time."""

    statement: bytes
    created: int


@dataclass(frozen=True)
class NewSession:
    """The session row stored at S7 (section 12), binary values as base64url text."""

    id: str
    passkey_id: str
    handle: str
    credential_id: str
    passkey_public_key: str
    session_public_key: str
    statement: str
    authenticator_data: str
    client_data_json: str
    signature: str
    created: int
    expires: int


def _invite(row: sqlite3.Row) -> Invite:
    return Invite(
        id=row["id"],
        handle=row["handle"],
        team=row["team"],
        created=row["created"],
        expires=row["expires"],
        created_by=row["created_by"],
        bootstrap=row["bootstrap"] == 1,
        opened=row["opened"],
        received=row["received"],
        credential_id=row["credential_id"],
        public_key=row["public_key"],
        proposed_id=row["proposed_id"],
        finished=row["finished"],
    )


def stored_binary(text: object) -> bytes:
    """A stored base64url value. The hub stores every binary value as canonical base64url text, so
    anything else (also a missing value) is a broken row and fails loudly."""
    raw = b64url_decode(text)
    if raw is None:
        raise ValueError("a stored binary value is not canonical base64url")
    return raw


def proposed_id(handle: str, taken: set[str]) -> str:
    """Section 5.6: `<handle>-passkey-<n>` with the smallest `n` from 1 that is not taken."""
    number = 1
    while f"{handle}-passkey-{number}" in taken:
        number += 1
    return f"{handle}-passkey-{number}"


class PasskeyStore:
    """The passkey tables of one hub store. Every method that acts at a time takes that time, except
    `open_invite`, which takes the clock and reads it inside its transaction."""

    def __init__(self, store: "Store") -> None:
        self.store = store

    # --- invites (sections 5.1 to 5.6) -----------------------------------------------------------

    def create_invite(self, invite: NewInvite, now: int) -> str:
        """I2 (and for a bootstrap invite first check 3 of section 5.3) with the insert, in one
        write transaction. Returns `created`, `bootstrap_open` or `invite_limit`."""
        with self.store.transaction() as connection:
            if invite.bootstrap:
                open_bootstrap = connection.execute(
                    f"SELECT COUNT(*) AS n {_INVITES} WHERE i.bootstrap = 1 AND {_OPEN}",
                    (now, now, CEREMONY_SECONDS),
                ).fetchone()
                if open_bootstrap["n"] > 0:
                    return BOOTSTRAP_OPEN
            open_count = connection.execute(
                f"SELECT COUNT(*) AS n {_INVITES} WHERE {_OPEN}", (now, now, CEREMONY_SECONDS)
            ).fetchone()
            if open_count["n"] >= INVITE_OPEN_LIMIT:
                return INVITE_LIMIT
            connection.execute(
                "INSERT INTO passkey_invite (id, token_hash, handle, team, created, expires, "
                "created_by, bootstrap, creation_request) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invite.id,
                    invite.token_hash,
                    invite.handle,
                    invite.team,
                    invite.created,
                    invite.expires,
                    invite.created_by,
                    1 if invite.bootstrap else 0,
                    invite.creation_request,
                ),
            )
        return CREATED

    def invites(self) -> list[Invite]:
        """Every invite, newest first by its latest recorded change (section 5.2): `received`, else
        the `finished` time of its registration, else `opened`, else `created`; then invite id
        descending."""
        rows = self.store.query(
            f"SELECT {_INVITE_COLUMNS} {_INVITES} "
            "ORDER BY COALESCE(i.received, r.finished, i.opened, i.created) DESC, i.id DESC"
        )
        return [_invite(row) for row in rows]

    def open_invite(
        self, token_hash: str, clock: Clock, *, registration_id: str, challenge: str, user_id: str
    ) -> Invite | str:
        """O2 to O4 and the insert of the registration, in one write transaction (section 5.4).

        Returns the opened invite, or `invite_unknown`, `invite_expired` or `invite_used`. O3 and O4
        use the same now, read from `clock` once the transaction has begun, so that the wait for the
        store lock and for SQLite's write lock lies before the check (section 12, "Clock"). The
        update of O4 marks the invite opened only when it was not opened before and is not expired,
        so an expired invite is never marked opened.
        """
        with self.store.transaction() as connection:
            now = clock()
            row = connection.execute(
                f"SELECT {_INVITE_COLUMNS} {_INVITES} WHERE i.token_hash = ?", (token_hash,)
            ).fetchone()
            if row is None:
                return INVITE_UNKNOWN
            invite = _invite(row)
            if now >= invite.expires:
                return INVITE_EXPIRED
            marked = connection.execute(
                "UPDATE passkey_invite SET opened = ? WHERE id = ? AND opened IS NULL AND expires > ?",
                (now, invite.id, now),
            )
            if marked.rowcount != 1:
                return INVITE_USED
            connection.execute(
                "INSERT INTO passkey_registration (id, invite_id, challenge, user_id, created, finished) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (registration_id, invite.id, challenge, user_id, now),
            )
        return replace(invite, opened=now)

    def consume_registration(self, registration_id: str, now: int) -> Registration | None:
        """R2: one atomic update marks the registration finished, committed on its own. None when
        it does not exist or was finished before."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT r.id, r.invite_id, r.challenge, i.opened, i.expires, i.handle, i.bootstrap "
                "FROM passkey_registration r JOIN passkey_invite i ON i.id = r.invite_id "
                "WHERE r.id = ? AND r.finished IS NULL",
                (registration_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE passkey_registration SET finished = ? WHERE id = ? AND finished IS NULL",
                (now, registration_id),
            )
        return Registration(
            id=row["id"],
            invite_id=row["invite_id"],
            challenge=stored_binary(row["challenge"]),
            opened=row["opened"],
            expires=row["expires"],
            handle=row["handle"],
            bootstrap=row["bootstrap"] == 1,
        )

    def receive_invite(
        self,
        registration: Registration,
        *,
        credential_id: bytes,
        public_key: bytes,
        keys: "KeyRing",
        now: int,
    ) -> str | None:
        """R14, the choice of the proposed keys-file id (section 5.6) and the storing of the
        received key, in one write transaction. Returns the proposed id, or None when the credential
        id or the public key is in the keys file or in another received invite (`credential_known`).
        The keys file is the one the hub loaded at its start.
        """
        if any(
            entry.credential_id == credential_id or entry.public_key == public_key
            for entry in keys.entries.values()
        ):
            return None
        credential_text = b64url_encode(credential_id)
        public_key_text = b64url_encode(public_key)
        with self.store.transaction() as connection:
            received = connection.execute(
                "SELECT credential_id, public_key, proposed_id FROM passkey_invite "
                "WHERE received IS NOT NULL"
            ).fetchall()
            if any(
                row["credential_id"] == credential_text or row["public_key"] == public_key_text
                for row in received
            ):
                return None
            chosen = proposed_id(
                registration.handle, set(keys.entries) | {row["proposed_id"] for row in received}
            )
            connection.execute(
                "UPDATE passkey_invite SET received = ?, credential_id = ?, public_key = ?, proposed_id = ? "
                "WHERE id = ?",
                (now, credential_text, public_key_text, chosen, registration.invite_id),
            )
        return chosen

    # --- sign-in (section 6) ---------------------------------------------------------------------

    def begin_sign_in(
        self, sign_in_id: str, *, statement: str, session_public_key: str, now: int
    ) -> str | None:
        """B2 and B3 with the insert of the pending sign-in, in one write transaction. Pending
        sign-ins older than `CEREMONY_SECONDS` are deleted first, and the deletion is committed
        also when B2 or B3 refuses. Returns None, `session_key_known` or `too_many_pending`."""
        with self.store.transaction() as connection:
            connection.execute(
                "DELETE FROM passkey_sign_in WHERE created < ?", (now - CEREMONY_SECONDS,)
            )
            known = connection.execute(
                "SELECT (SELECT COUNT(*) FROM passkey_session WHERE session_public_key = ?) "
                "+ (SELECT COUNT(*) FROM passkey_sign_in WHERE session_public_key = ?) AS n",
                (session_public_key, session_public_key),
            ).fetchone()
            if known["n"] > 0:
                return SESSION_KEY_KNOWN
            pending = connection.execute("SELECT COUNT(*) AS n FROM passkey_sign_in").fetchone()
            if pending["n"] >= PENDING_SIGN_IN_LIMIT:
                return TOO_MANY_PENDING
            connection.execute(
                "INSERT INTO passkey_sign_in (id, statement, session_public_key, created) VALUES (?, ?, ?, ?)",
                (sign_in_id, statement, session_public_key, now),
            )
        return None

    def take_sign_in(self, sign_in_id: str) -> PendingSignIn | None:
        """S2: the pending sign-in is read and deleted in one atomic step, committed on its own."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT statement, created FROM passkey_sign_in WHERE id = ?", (sign_in_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM passkey_sign_in WHERE id = ?", (sign_in_id,))
        return PendingSignIn(statement=stored_binary(row["statement"]), created=row["created"])

    def create_session(self, session: NewSession, sign_count: int) -> None:
        """S7 (section 4.4) with the update of the counter and the insert of the session, in one
        write transaction. Raises `counter_regressed`; a failed insert leaves the counter as it
        was."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT sign_count FROM passkey_counter WHERE passkey_id = ?", (session.passkey_id,)
            ).fetchone()
            kept = check_counter(None if row is None else row["sign_count"], sign_count)
            connection.execute(
                "INSERT INTO passkey_counter (passkey_id, sign_count) VALUES (?, ?) "
                "ON CONFLICT(passkey_id) DO UPDATE SET sign_count = excluded.sign_count",
                (session.passkey_id, kept),
            )
            connection.execute(
                "INSERT INTO passkey_session (id, passkey_id, handle, credential_id, passkey_public_key, "
                "session_public_key, statement, authenticator_data, client_data_json, signature, created, "
                "expires, ended) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    session.id,
                    session.passkey_id,
                    session.handle,
                    session.credential_id,
                    session.passkey_public_key,
                    session.session_public_key,
                    session.statement,
                    session.authenticator_data,
                    session.client_data_json,
                    session.signature,
                    session.created,
                    session.expires,
                ),
            )

    # --- sessions and commands (sections 7 and 8) ------------------------------------------------

    def find_session(self, session_id: str) -> SessionKey | None:
        """The read of check 2 (section 7.1): the stored session of the id that was not ended."""
        row = self.store.query_one(
            "SELECT id, passkey_id, handle, credential_id, passkey_public_key, session_public_key, expires "
            "FROM passkey_session WHERE id = ? AND ended IS NULL",
            (session_id,),
        )
        if row is None:
            return None
        return SessionKey(
            id=row["id"],
            passkey_id=row["passkey_id"],
            handle=row["handle"],
            credential_id=stored_binary(row["credential_id"]),
            passkey_public_key=stored_binary(row["passkey_public_key"]),
            session_public_key=stored_binary(row["session_public_key"]),
            expires=row["expires"],
        )

    def end_session(self, session_id: str, now: int) -> None:
        """Section 7.3: sets the session's `ended` time; a time already set is kept."""
        self.store.execute(
            "UPDATE passkey_session SET ended = ? WHERE id = ? AND ended IS NULL", (now, session_id)
        )

    def advance_sequence(self, sender: str, sequence: int) -> bool:
        """C6: the sequence is higher than the last one accepted from the sender; the comparison and
        the update are one write transaction. False for `sequence_not_higher`."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM command_sequence WHERE sender = ?", (sender,)
            ).fetchone()
            if row is not None and sequence <= row["last_sequence"]:
                return False
            connection.execute(
                "INSERT INTO command_sequence (sender, last_sequence) VALUES (?, ?) "
                "ON CONFLICT(sender) DO UPDATE SET last_sequence = excluded.last_sequence",
                (sender, sequence),
            )
        return True
