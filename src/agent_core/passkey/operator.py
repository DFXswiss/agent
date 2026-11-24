"""The operator commands `invite-bootstrap` and `invite-list` (PASSKEY-PROTOCOL.md section 5.3).

Both read the same environment variables as the hub and use the same database. When the
configuration (also the record's variables of step `config` of RECORD-PROTOCOL.md section 11.4),
the teams file or the keys file breaks a rule that stops the hub at start, either
command prints the reason to standard error, writes nothing and exits with status 1. A usage
error is decided in `run_command` (`UsageError`, `EXIT_USAGE`, status 2) before anything is read.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from typing import Sequence, TextIO

from ..config import Config, ConfigError
from ..db import Store
from ..keys import INVITE_SCOPE, KeyRing, KeysError, load_keys
from ..record.runtime import RecordConfigError, record_config
from ..signing import Clock, format_time
from ..teams import Roster, TeamsError, load_roster
from .encoding import INVITE_ID_RANDOM_BYTES, INVITE_TOKEN_BYTES, invite_id, invite_token
from .invites import OPERATOR, invite_entry, invite_fields, invite_link, token_hash
from .relying_party import relying_party
from .storage import BOOTSTRAP_OPEN, CREATED, NewInvite, PasskeyStore

COMMAND_BOOTSTRAP = "invite-bootstrap"
COMMAND_LIST = "invite-list"
USAGE = (
    "usage: python -m agent_core "
    "[invite-bootstrap --handle HANDLE --team TEAM --seconds N | invite-list]"
)
BOOTSTRAP_OPTIONS = ("--handle", "--team", "--seconds")
# `--seconds` is a decimal integer without sign and without leading zero, like a JSON integer.
_SECONDS_RE = re.compile(r"0|[1-9][0-9]{0,15}")

EXIT_REFUSED = 1
EXIT_USAGE = 2
# What `_hub_inputs` raises when a rule stops the hub at start: status 1 with the reason.
HUB_START_ERRORS = (ConfigError, TeamsError, KeysError, RecordConfigError)


class UsageError(Exception):
    """An unknown or repeated option, or an option without its value: status 2."""


@dataclass(frozen=True)
class Streams:
    """Standard output and standard error of a command."""

    out: TextIO
    err: TextIO


def bootstrap_options(args: Sequence[str]) -> dict[str, str]:
    """The options of `invite-bootstrap`, each at most once and each followed by its value.

    Raises `UsageError` for an unknown or repeated option and for an option without a value. A
    missing option is no usage error: it breaks I1 and is refused with status 1.
    """
    options: dict[str, str] = {}
    position = 0
    while position < len(args):
        name = args[position]
        if name not in BOOTSTRAP_OPTIONS or name in options or position + 1 == len(args):
            raise UsageError(name)
        options[name] = args[position + 1]
        position += 2
    return options


def _hub_inputs(clock: Clock) -> tuple[Config, Roster, KeyRing]:
    """The configuration, the teams file and the keys file, read and checked as at the hub's start
    and in its order (the keys file from `AGENT_CORE_KEYS` now, not the copy a running hub loaded).

    The checks are the functions the start calls: `Config.from_env` (which also holds the rules of
    the hub key that `HubSigner.from_seed_hex` repeats), `load_roster`, `load_keys`, and step
    `config` of the record start (`record_config`, RECORD-PROTOCOL.md section 11.4). The later
    steps of the record start need a connection to the record database and are no rule of the
    configuration. Raises `ConfigError`, `TeamsError`, `KeysError` or `RecordConfigError` with the
    reason."""
    cfg = Config.from_env()
    roster = load_roster(cfg.teams_path)
    keys = load_keys(cfg.keys_path, roster, clock())
    record_config(os.environ)
    return cfg, roster, keys


def run_bootstrap(options: dict[str, str], streams: Streams, clock: Clock) -> int:
    """`invite-bootstrap`: checks 1 to 4 of section 5.3 in this order, then the invite.

    Writes the line `invite bootstrap created <invite id> <handle> <team> <expires>` to standard
    error and only the link to standard output.
    """
    try:
        cfg, roster, keys = _hub_inputs(clock)
    except HUB_START_ERRORS as exc:
        print(str(exc), file=streams.err)
        return EXIT_REFUSED
    seconds = options.get("--seconds")
    lifetime = int(seconds) if seconds is not None and _SECONDS_RE.fullmatch(seconds) else None
    fields = invite_fields(options.get("--handle"), options.get("--team"), lifetime, roster)
    if fields is None:
        print("invite-bootstrap refused: the arguments break the rules of an invite", file=streams.err)
        return EXIT_REFUSED
    if any(INVITE_SCOPE in entry.scopes for entry in keys.entries.values()):
        print(f"invite-bootstrap refused: an entry of the keys file holds {INVITE_SCOPE}", file=streams.err)
        return EXIT_REFUSED
    now = clock()
    token = secrets.token_bytes(INVITE_TOKEN_BYTES)
    invite = NewInvite(
        id=invite_id(secrets.token_bytes(INVITE_ID_RANDOM_BYTES)),
        token_hash=token_hash(token),
        handle=fields.handle,
        team=fields.team,
        created=now,
        expires=now + fields.lifetime_seconds,
        created_by=OPERATOR,
        bootstrap=True,
        creation_request=None,
    )
    store = Store(cfg.database)
    try:
        outcome = PasskeyStore(store).create_invite(invite, now)
    finally:
        store.close()
    if outcome == BOOTSTRAP_OPEN:
        print("invite-bootstrap refused: another bootstrap invite is open", file=streams.err)
        return EXIT_REFUSED
    if outcome != CREATED:
        print("invite-bootstrap refused: the limit of open invites is reached", file=streams.err)
        return EXIT_REFUSED
    print(
        f"invite bootstrap created {invite.id} {invite.handle} {invite.team} {format_time(invite.expires)}",
        file=streams.err,
    )
    print(invite_link(relying_party(cfg.public_url).origin, invite_token(token)), file=streams.out)
    return 0


def run_list(streams: Streams, clock: Clock) -> int:
    """`invite-list`: the list of section 5.2 as JSON, one invite per line, read directly from the
    database. It is the binding source of the keys-file text (section 5.6)."""
    try:
        cfg, _, _ = _hub_inputs(clock)
    except HUB_START_ERRORS as exc:
        print(str(exc), file=streams.err)
        return EXIT_REFUSED
    store = Store(cfg.database)
    try:
        invites = PasskeyStore(store).invites()
    finally:
        store.close()
    now = clock()
    for invite in invites:
        print(json.dumps(invite_entry(invite, now), separators=(",", ":")), file=streams.out)
    return 0


def run_command(args: Sequence[str], streams: Streams, clock: Clock) -> int:
    """The command of the first argument, `invite-bootstrap` or `invite-list`. Any other first
    argument, an unknown or repeated option, an option without its value and an argument after
    `invite-list` print the usage line to standard error and give status 2, before anything is
    read."""
    command, rest = args[0], args[1:]
    try:
        if command == COMMAND_BOOTSTRAP:
            options = bootstrap_options(rest)
        elif command != COMMAND_LIST or rest:
            raise UsageError(command)
    except UsageError:
        print(USAGE, file=streams.err)
        return EXIT_USAGE
    if command == COMMAND_BOOTSTRAP:
        return run_bootstrap(options, streams, clock)
    return run_list(streams, clock)


def system_streams() -> Streams:
    return Streams(out=sys.stdout, err=sys.stderr)
