"""Event rules E1-E9 (04-crypto.md sections 4.3, 5.12, 6.4) and the kind matrix (CON-03).

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

An agent (ADR-082) writes with its user's rights: E3 treats it as a user, E5 resolves its rights through
`OrgState.acting`, and the `ref` of its `member.profile` is its user.

Support (ADR-096): an author that is no principal of the org but a person of the `support` roster passed in (readers:
`PlatformState.support_authors`, the hub: `support_roster`) has the author kind `support`; E3 treats it as a user for
the workgroup kinds of `SUPPORT_KINDS` only (`prompt`, workgroup whiteboard; `kind_not_allowed` otherwise), E5 needs
a `write` release of the workgroup at `lh` (and at the hub at receipt). A member of the org that is also a support
person writes if either its member right or the release allows it (the org log first, then the roster).
`support.info` (the names a support person needs, sealed under the workgroup key) is written by a principal that may
release the workgroup (`wg_release`).

Every function takes the clock as a parameter (`now`, `rotated_at`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

from agent_hub.crypto import event as event_format
from agent_hub.crypto.encoding import b64u_decode, canonical, require_keys
from agent_hub.crypto.errors import fail
from agent_hub.crypto.ids import is_uuid7
from agent_hub.log.state import AGENT, DEVICE, SUPPORT, USER, OrgState, Principal, Roster
from agent_hub.log.verify import VerifiedLog

GRACE = timedelta(seconds=600)
"""E6: the previous epoch is accepted this long after the rotation (not for removed authors)."""

ORG_ADMIN = "org_admin"
ORG_MEMBER_SELF = "org_member_self"
WG_WRITE = "wg_write"
DEVICE_ASSIGNED_WG = "device_assigned_wg"
DEVICE_ASSIGNED_WG_SESSION = "device_assigned_wg_session"
WG_RELEASE = "wg_release"
RIGHTS = {
    ORG_ADMIN: "author is an admin of the org",
    ORG_MEMBER_SELF: "author is an org member and ref == author",
    WG_WRITE: "author has write access to the workgroup",
    DEVICE_ASSIGNED_WG: "author device is assigned to the workgroup",
    DEVICE_ASSIGNED_WG_SESSION: "author device is assigned to the workgroup and the session runs on it",
    WG_RELEASE: "author is an admin of the org or has write access to the workgroup, which is not archived",
}
NOTIFY_ALLOWED = ("whiteboard.entry",)
SUPPORT_KINDS = frozenset({"prompt", "whiteboard.entry", "whiteboard.edit", "whiteboard.delete"})
"""What a support person writes with a `write` release (ADR-096 section 4): prompts into existing tasks and the
workgroup whiteboard - no task, no workgroup settings."""
SIZE_LIMITS = {"default_ct_bytes": event_format.MAX_CT_BYTES, "session.terminal": event_format.MAX_TERMINAL_CT_BYTES}
_EVENT_KEYS = frozenset({"header", "ct", "sig"})


@dataclass(frozen=True)
class KindRule:
    """One row of the matrix: `kind` in `scope`, written by one of `authors`, needs `right`; `header` constrains
    header fields (`empty` = "", `set` = a UUID, `scope_id` = equals scope_id, `target` = a workgroup of the org,
    `author` = equals the author, `device`/`entry`/`operation` = a UUID, `read_target` = not empty,
    `subject` = a user of the org or a user invitation of its log at `lh`)."""

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
    # an admin-given name of a member or of an invitation's person (ADR-073)
    KindRule("member.name", "org", (USER,), ORG_ADMIN, {"wg": "empty", "ref": "subject"}),
    *(
        rule
        for kind in ("whiteboard.entry", "whiteboard.edit", "whiteboard.delete")
        for rule in (
            KindRule(kind, "org", (USER,), ORG_ADMIN, _ORG_ENTRY, _ORG_LEVEL),
            KindRule(kind, "wg", (USER,), WG_WRITE, _WG_ENTRY, _WG_LEVEL),
        )
    ),
    KindRule("settings.workgroup", "wg", (USER,), WG_WRITE, {"wg": "scope_id"}),
    # the names a support person needs (org, workgroup, members), sealed under the workgroup key (ADR-096)
    KindRule(
        "support.info",
        "wg",
        (USER,),
        WG_RELEASE,
        {"wg": "scope_id", "task": "empty", "session": "empty", "ref": "empty"},
        "written by the releasing client with a support release",
    ),
    KindRule("task.meta", "wg", (USER,), WG_WRITE, _WG_TASK),
    KindRule(
        "task.meta", "wg", (DEVICE,), DEVICE_ASSIGNED_WG, _WG_TASK, "only as result of the operation task.set_title"
    ),
    # the name of the task's main agent (ADR-095 section 2): written by people only, with the right of the title
    KindRule("task.agent_name", "wg", (USER,), WG_WRITE, _WG_TASK),
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
    KindRule("test.run", "wg", (DEVICE,), DEVICE_ASSIGNED_WG_SESSION, _SESSION),
    # Platform browser (ADR-085, 13-work-vm.md section 13.5): every action of a session's browser and its lifecycle
    KindRule("browser.action", "wg", (DEVICE,), DEVICE_ASSIGNED_WG_SESSION, _SESSION),
    KindRule("browser.state", "wg", (DEVICE,), DEVICE_ASSIGNED_WG_SESSION, _SESSION),
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


def _is_subject(value: object, state: OrgState) -> bool:
    """A user of the org (`principals`, active or not) or a user invitation of its log, in the state at `lh`."""
    # the header format (E1) makes `ref` a string
    principal = state.principals.get(cast(str, value))
    invite = state.invites.get(cast(str, value))
    return (principal is not None and principal.kind == USER) or (invite is not None and invite.kind == USER)


def _field_valid(rule: str, value: object, header: Mapping[str, object], state: OrgState) -> bool:
    if rule == "empty":
        return value == ""
    if rule == "scope_id":
        return value == header["scope_id"]
    if rule == "target":
        return value in state.workgroups
    if rule == "author":  # an agent's profile is its user's
        return value == state.acting(cast(str, header["author"]))
    if rule in ("read_target", "subject"):
        return value != "" if rule == "read_target" else _is_subject(value, state)
    # set, device, entry, operation: a UUID
    return is_uuid7(value)


def check_header_rules(rule: KindRule, header: Mapping[str, object], state: OrgState) -> None:
    """The kind's header field rules (`header_invalid`)."""
    if not all(_field_valid(check, header[name], header, state) for name, check in rule.header.items()):
        fail("header_invalid")


def has_right(rule: KindRule, state: OrgState, header: Mapping[str, object], support: Roster | None = None) -> bool:
    """E5: the right of the rule for the header's author in `state`; `support` the roster of support authors."""
    author, scope_id = cast(str, header["author"]), cast(str, header["scope_id"])
    if rule.right == ORG_ADMIN:
        return state.is_admin(author)
    if rule.right == ORG_MEMBER_SELF:
        return state.member(author) is not None and header["ref"] == state.acting(author)
    if rule.right == WG_WRITE:
        # the member right first, then the support release (a member that is also support, ADR-096)
        return state.can_write_wg(author, scope_id) or (
            header["kind"] in SUPPORT_KINDS and state.support_writes(support or {}, author, scope_id)
        )
    if rule.right == WG_RELEASE:
        return state.writable(scope_id) and state.can_release(author, scope_id)
    return state.assigned(author, scope_id) and state.writable(scope_id)


def _author(
    state: OrgState, header: Mapping[str, object], excluded: Iterable[str], support: Roster | None = None
) -> Principal:
    author = cast(str, header["author"])
    found = state.member(author)
    person = (support or {}).get(author)
    if found is None and person is not None:
        # a support person (ADR-096): no principal of the org, keys from the root certification
        found = Principal(author, SUPPORT, SUPPORT, person.kem, person.sig, 0)
    if found is None or found.id in set(excluded):
        fail("author_not_writer")
    return found


def _rule(header: Mapping[str, object], author: Principal) -> KindRule:
    """E3 for the author's kind: an agent as a user; a support person as a user for workgroup writes only."""
    kind, scope = cast(str, header["kind"]), cast(str, header["scope"])
    rule = kind_rule(kind, scope, USER if author.kind in (AGENT, SUPPORT) else author.kind)
    if author.kind == SUPPORT and (rule.scope != "wg" or kind not in SUPPORT_KINDS):
        fail("kind_not_allowed")
    return rule


def _session_device(rule: KindRule, header: Mapping[str, object], session_device: str | None) -> None:
    if rule.right == DEVICE_ASSIGNED_WG_SESSION and session_device is not None and session_device != header["author"]:
        fail("author_not_writer")


def check_event(
    event: object,
    log: VerifiedLog,
    *,
    session_device: str | None = None,
    excluded: Iterable[str] = (),
    support: Roster | None = None,
) -> tuple[dict[str, object], KindRule]:
    """Reader checks (steps 1-6, and E7 when the reader knows the session's device); returns header and rule.
    `support`: the support authors of the reader's platform log (ADR-096)."""
    data = require_keys(event, _EVENT_KEYS)
    header = event_format.check_header(data["header"])
    if header["org"] != log.state.org:
        fail("header_invalid")
    state = log.state_at(cast(int, header["lh"]))
    author = _author(state, header, excluded, support)
    rule = _rule(header, author)
    check_header_rules(rule, header, state)
    event_format.verify_event(event, b64u_decode(author.sig))
    if not has_right(rule, state, header, support):
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
    support: Roster | None = None,
) -> None:
    """E6 at the hub: the current epoch, or the previous one within `GRACE` after the rotation at `rotated_at` if the
    author is entitled to the new epoch as well (`epoch_stale`; a support author: by the release, ADR-096)."""
    scope, scope_id, author = cast(str, header["scope"]), cast(str, header["scope_id"]), cast(str, header["author"])
    current = state.epoch(scope, scope_id)
    epoch = header["epoch"]
    in_grace = (
        epoch == current - 1
        and rotated_at is not None
        and now - rotated_at <= GRACE
        and (state.can_read(author, scope, scope_id) or state.support_reads(support or {}, author, scope_id))
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
    support: Roster | None = None,
) -> dict[str, object]:
    """The hub's checks: reader checks, E5 at receipt, E6, E7. `rotated_at` is the receipt time of the entry that
    created the scope's current epoch; `session_device` the device the header's session runs on (None if the kind
    has no session or the session is unknown, which fails E7 for session kinds); `support` the current support roster
    (ADR-096: a person that left support writes nothing new)."""
    skip, people = set(excluded), support or {}
    header, rule = check_event(event, log, excluded=skip, support=people)
    state = log.state
    author = cast(str, header["author"])
    if (state.member(author) is None and author not in people) or not has_right(rule, state, header, people):
        fail("author_not_writer")
    check_epoch(state, header, now=now, rotated_at=rotated_at, excluded=skip, support=people)
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
