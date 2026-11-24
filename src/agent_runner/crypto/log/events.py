"""Copied from agent-core src/agent_hub/log/events.py at 56db2db59b0e.

Event rules E1-E9 (04-crypto.md sections 4.3, 5.12, 6.4) and the kind matrix (CON-03).

`KIND_RULES` is the table of section 4.3 and equal to `contracts/enums/event-kinds.json` (a test compares them).
E1, E2, E4 and E8 are the format checks of `agent_hub.crypto.event`; this module adds the rest. Check order (every
implementation; the first failing check gives the code):

1. E1/E2 object and header format (`header_invalid`, `non_ascii`), header `org` equals the log's org
   (`header_invalid`);
2. L10 `lh` within the verified log (`log_fork`; readers call `verify.needs_refresh` and refresh first);
3. author: an active principal of the org at `lh`, not excluded (`author_not_writer`; `excluded` =
   `PlatformState.excluded(state)`, the platform devices whose certification has ended: the hub always passes it,
   readers for live events, and may leave it out to show the history of a device decertified later);
4. E3 `(kind, scope, author kind)` allowed (`kind_not_allowed`), then the kind's header rules (`header_invalid`);
5. E8 size (`size_exceeded`) and E4 author signature under the author's key at `lh` (`signature_invalid`);
6. E5 the kind's right at `lh` (`author_not_writer`; no event is written into an archived workgroup);
7. hub only: E5 again at receipt (`author_not_writer`), E6 epoch (`epoch_stale`), E7 the session runs on the
   author device (`author_not_writer`). Readers accept any epoch they hold a key for. E9 (`event_conflict`) is
   `check_replay`, run by the hub against the stored event with the same `id`.

Every function takes the clock as a parameter (`now`, `rotated_at`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from agent_runner.crypto import event as event_format
from agent_runner.crypto.encoding import b64u_decode, canonical, require_keys
from agent_runner.crypto.errors import fail
from agent_runner.crypto.ids import is_uuid7
from agent_runner.crypto.log.state import DEVICE, USER, OrgState, Principal
from agent_runner.crypto.log.verify import VerifiedLog

GRACE = timedelta(seconds=600)
"""E6: the previous epoch is accepted this long after the rotation (not for removed authors)."""

ORG_ADMIN = "org_admin"
ORG_MEMBER_SELF = "org_member_self"
WG_WRITE = "wg_write"
DEVICE_ASSIGNED_WG = "device_assigned_wg"
DEVICE_ASSIGNED_WG_SESSION = "device_assigned_wg_session"
RIGHTS = {
    ORG_ADMIN: "author is an admin of the org",
    ORG_MEMBER_SELF: "author is an org member and ref == author",
    WG_WRITE: "author has write access to the workgroup",
    DEVICE_ASSIGNED_WG: "author device is assigned to the workgroup",
    DEVICE_ASSIGNED_WG_SESSION: "author device is assigned to the workgroup and the session runs on it",
}
NOTIFY_ALLOWED = ("whiteboard.entry",)
SIZE_LIMITS = {"default_ct_bytes": event_format.MAX_CT_BYTES, "session.terminal": event_format.MAX_TERMINAL_CT_BYTES}
_EVENT_KEYS = frozenset({"header", "ct", "sig"})


@dataclass(frozen=True)
class KindRule:
    """One row of the matrix: `kind` in `scope`, written by one of `authors`, needs `right`; `header` constrains
    header fields (`empty` = "", `set` = a UUID, `scope_id` = equals scope_id, `target` = a workgroup of the org,
    `author` = equals the author, `device`/`entry`/`operation` = a UUID, `read_target` = not empty)."""

    kind: str
    scope: str
    authors: tuple[str, ...]
    right: str
    header: Mapping[str, str] = field(default_factory=dict)
    note: str = ""


_ORG_ENTRY = {"wg": "empty", "task": "empty", "ref": "entry"}
_WG_ENTRY = {"wg": "scope_id", "ref": "entry"}
_WG_TASK = {"wg": "scope_id", "task": "set", "session": "empty", "ref": "empty"}
_SESSION = {"wg": "scope_id", "task": "set", "session": "set"}
_ORG_LEVEL = "org level"
_WG_LEVEL = "wg level: task empty; task level: task set"

KIND_RULES: tuple[KindRule, ...] = (
    KindRule("org.profile", "org", (USER,), ORG_ADMIN, {"wg": "empty"}),
    KindRule("wg.profile", "org", (USER,), ORG_ADMIN, {"wg": "target"}),
    KindRule("member.profile", "org", (USER,), ORG_MEMBER_SELF, {"wg": "empty", "ref": "author"}),
    KindRule("device.profile", "org", (USER,), ORG_ADMIN, {"wg": "empty", "ref": "device"}),
    *(
        rule
        for kind in ("whiteboard.entry", "whiteboard.edit", "whiteboard.delete")
        for rule in (
            KindRule(kind, "org", (USER,), ORG_ADMIN, _ORG_ENTRY, _ORG_LEVEL),
            KindRule(kind, "wg", (USER,), WG_WRITE, _WG_ENTRY, _WG_LEVEL),
        )
    ),
    KindRule("settings.workgroup", "wg", (USER,), WG_WRITE, {"wg": "scope_id"}),
    KindRule("task.meta", "wg", (USER,), WG_WRITE, _WG_TASK),
    KindRule(
        "task.meta", "wg", (DEVICE,), DEVICE_ASSIGNED_WG, _WG_TASK, "only as result of the operation task.set_title"
    ),
    KindRule("prompt", "wg", (USER,), WG_WRITE, _WG_TASK),
    *(
        KindRule(f"session.{name}", "wg", (DEVICE,), DEVICE_ASSIGNED_WG_SESSION, _SESSION)
        for name in ("output", "tool", "hook", "file", "terminal", "status", "error")
    ),
    KindRule("operation.params", "wg", (DEVICE,), DEVICE_ASSIGNED_WG, {"wg": "scope_id", "ref": "operation"}),
    KindRule("operation.result", "wg", (DEVICE,), DEVICE_ASSIGNED_WG, {"wg": "scope_id", "ref": "operation"}),
    KindRule("platform.read", "wg", (DEVICE,), DEVICE_ASSIGNED_WG, {"wg": "scope_id", "ref": "read_target"}),
    # Work VMs (ADR-059, 13-work-vm.md section 8): runs and lifecycle of a session's work VM
    KindRule("workvm.run", "wg", (DEVICE,), DEVICE_ASSIGNED_WG_SESSION, _SESSION),
    KindRule("workvm.state", "wg", (DEVICE,), DEVICE_ASSIGNED_WG_SESSION, _SESSION),
)
_BY_KEY = {(rule.kind, rule.scope, author): rule for rule in KIND_RULES for author in rule.authors}


def matrix() -> dict[str, object]:
    """The matrix in the form of `contracts/enums/event-kinds.json` (without its description)."""
    return {
        "rights": dict(RIGHTS),
        "notify_allowed": list(NOTIFY_ALLOWED),
        "size_limits": dict(SIZE_LIMITS),
        "kinds": [
            {
                "kind": rule.kind,
                "scope": rule.scope,
                "authors": list(rule.authors),
                "right": rule.right,
                "header": dict(rule.header),
                **({"note": rule.note} if rule.note else {}),
            }
            for rule in KIND_RULES
        ],
    }


def kind_rule(kind: str, scope: str, author_kind: str) -> KindRule:
    """E3: the rule of an allowed combination (`kind_not_allowed` otherwise)."""
    rule = _BY_KEY.get((kind, scope, author_kind))
    if rule is None:
        fail("kind_not_allowed")
    return rule


def _field_valid(rule: str, value: object, header: Mapping[str, object], state: OrgState) -> bool:
    if rule == "empty":
        return value == ""
    if rule == "scope_id":
        return value == header["scope_id"]
    if rule == "target":
        return value in state.workgroups
    if rule == "author":
        return value == header["author"]
    if rule == "read_target":
        return value != ""
    # set, device, entry, operation: a UUID
    return is_uuid7(value)


def check_header_rules(rule: KindRule, header: Mapping[str, object], state: OrgState) -> None:
    """The kind's header field rules (`header_invalid`)."""
    if not all(_field_valid(check, header[name], header, state) for name, check in rule.header.items()):
        fail("header_invalid")


def has_right(rule: KindRule, state: OrgState, header: Mapping[str, object]) -> bool:
    """E5: the right of the rule for the header's author in `state`."""
    author, scope_id = cast(str, header["author"]), cast(str, header["scope_id"])
    if rule.right == ORG_ADMIN:
        return state.is_admin(author)
    if rule.right == ORG_MEMBER_SELF:
        return state.member(author) is not None and header["ref"] == author
    if rule.right == WG_WRITE:
        return state.can_write_wg(author, scope_id)
    return state.assigned(author, scope_id) and state.writable(scope_id)


def _author(state: OrgState, header: Mapping[str, object], excluded: Iterable[str]) -> Principal:
    found = state.member(cast(str, header["author"]))
    if found is None or found.id in set(excluded):
        fail("author_not_writer")
    return found


def _session_device(rule: KindRule, header: Mapping[str, object], session_device: str | None) -> None:
    if rule.right == DEVICE_ASSIGNED_WG_SESSION and session_device is not None and session_device != header["author"]:
        fail("author_not_writer")


def check_event(
    event: object, log: VerifiedLog, *, session_device: str | None = None, excluded: Iterable[str] = ()
) -> tuple[dict[str, object], KindRule]:
    """Reader checks (steps 1-6, and E7 when the reader knows the session's device); returns header and rule."""
    data = require_keys(event, _EVENT_KEYS)
    header = event_format.check_header(data["header"])
    if header["org"] != log.state.org:
        fail("header_invalid")
    state = log.state_at(cast(int, header["lh"]))
    author = _author(state, header, excluded)
    rule = kind_rule(cast(str, header["kind"]), cast(str, header["scope"]), author.kind)
    check_header_rules(rule, header, state)
    event_format.verify_event(event, b64u_decode(author.sig))
    if not has_right(rule, state, header):
        fail("author_not_writer")
    _session_device(rule, header, session_device)
    return header, rule


def check_epoch(
    state: OrgState,
    header: Mapping[str, object],
    *,
    now: datetime,
    rotated_at: datetime | None,
    excluded: Iterable[str] = (),
) -> None:
    """E6 at the hub: the current epoch, or the previous one within `GRACE` after the rotation at `rotated_at` if the
    author is entitled to the new epoch as well (`epoch_stale`)."""
    scope, scope_id, author = cast(str, header["scope"]), cast(str, header["scope_id"]), cast(str, header["author"])
    current = state.epoch(scope, scope_id)
    epoch = header["epoch"]
    in_grace = (
        epoch == current - 1
        and rotated_at is not None
        and now - rotated_at <= GRACE
        and state.can_read(author, scope, scope_id)
        and author not in set(excluded)
    )
    if epoch != current and not in_grace:
        fail("epoch_stale")


def check_event_hub(
    event: object,
    log: VerifiedLog,
    *,
    now: datetime,
    rotated_at: datetime | None,
    session_device: str | None,
    excluded: Iterable[str] = (),
) -> dict[str, object]:
    """The hub's checks: reader checks, E5 at receipt, E6, E7. `rotated_at` is the receipt time of the entry that
    created the scope's current epoch; `session_device` the device the header's session runs on (None if the kind
    has no session or the session is unknown, which fails E7 for session kinds)."""
    skip = set(excluded)
    header, rule = check_event(event, log, excluded=skip)
    state = log.state
    if state.member(cast(str, header["author"])) is None or not has_right(rule, state, header):
        fail("author_not_writer")
    check_epoch(state, header, now=now, rotated_at=rotated_at, excluded=skip)
    if rule.right == DEVICE_ASSIGNED_WG_SESSION and session_device != header["author"]:
        fail("author_not_writer")
    return header


def check_replay(stored: object | None, incoming: object) -> bool:
    """E9: True if `incoming` repeats the stored event with the same `id` byte for byte (idempotent success), False
    for a new `id`; different content under a known `id` is `event_conflict`."""
    if stored is None:
        return False
    if canonical(stored) != canonical(incoming):
        fail("event_conflict")
    return True
