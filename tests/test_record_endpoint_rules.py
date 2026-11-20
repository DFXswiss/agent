"""The rules of the reducer through the append endpoints (RECORD-PROTOCOL.md section 14, "The reducer",
and the other cases of section 14 that name a rule "through the endpoint").

Section 14 asks for every rule of the reducer through the endpoint and in the fold directly. The fold
directly is tests/test_record_reducer.py; here every event goes through `POST /api/key/record/events`
or `POST /api/key/record/watch` of the real application (`create_app`, `start_record`) against the
real PostgreSQL of AGENT_CORE_TEST_POSTGRES, and every case asserts the exact status, the exact body
bytes and the hub signature, with a fixed clock.

What section 14 asks that the keys file cannot express is named in the test that comes closest: an
`agent` or `staff` entry carries no `expires` (KEY-PROTOCOL.md section 3), so an `agent` key that is
expired at `received_at` cannot be written, and no staff key signs an event in this step (section 1),
so no task holds a `verification` value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Sequence

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agent_core.config import Config
from agent_core.record import commands
from agent_core.record.export import parse_export
from agent_core.record.verify import verify_record
from tests.conftest import RecordDatabase
from tests.record_helpers import migrated
from tests.record_hub_helpers import (
    ALICE,
    BOB,
    DAVE,
    LINK,
    OTHER_LINK,
    OTHER_REPO,
    OTHER_TEAM,
    REPO,
    SEEDS,
    TEAM,
    WATCHER,
    Hub,
    b64,
    error,
    key_entries,
    new_id,
    security_create,
    start_hub,
    task_create,
)
from tests.test_record_reducer import snapshot
from tests.test_signed_requests import NOW, fmt, public_text

EVENTS = "/api/key/record/events"
WATCH = "/api/key/record/watch"
OBJECT = "/api/key/record/object"

# The keys of this file beyond those of record_hub_helpers.key_entries: alice and una can act on a
# security issue; vic, wes, xia and yan can until their full key is revoked at REVOKED; wes then
# keeps only a `staff` entry, xia only a key without `record:security`, yan only a key without
# `record:write`; rita has no `record:write`, erin only a `staff` entry, rex only a key revoked
# before the test; `long` is the one key of a team of 100 bytes; the plain watcher has no
# `record:security`.
REVOKED = NOW + 600
AFTER_REVOCATION = NOW + 700
UNA = "una-laptop-1"
VIC = "vic-laptop-1"
WES = "wes-laptop-1"
XIA = "xia-laptop-1"
XIA_WITHOUT_SECURITY = "xia-laptop-2"
YAN = "yan-laptop-1"
YAN_WITHOUT_WRITE = "yan-laptop-2"
RITA = "rita-laptop-1"
REX = "rex-laptop-1"
LONG = "long-laptop-1"
PLAIN_WATCHER = "plain-watcher-1"
LONG_TEAM = "t" * 100
FULL = ["record:read", "record:write", "record:security", "device:controller"]
EXTRA_SEEDS = {
    UNA: 60,
    VIC: 61,
    WES: 62,
    XIA: 63,
    XIA_WITHOUT_SECURITY: 64,
    YAN: 65,
    YAN_WITHOUT_WRITE: 66,
    RITA: 67,
    REX: 68,
    LONG: 69,
    PLAIN_WATCHER: 70,
}
RULE_SEEDS = {**SEEDS, **EXTRA_SEEDS}


def _agent(
    key_id: str, handle: str, scopes: Sequence[str], *, team: str = TEAM, device: str = "laptop-1", revoked: int | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": key_id,
        "kind": "ed25519",
        "principal": "agent",
        "handle": handle,
        "team": team,
        "device": device,
        "public_key": public_text(EXTRA_SEEDS[key_id]),
        "scopes": list(scopes),
    }
    if revoked is not None:
        entry["revoked"] = fmt(revoked)
    return entry


def _staff(key_id: str, handle: str, number: int) -> dict[str, Any]:
    point = ec.derive_private_key(number, ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {
        "id": key_id,
        "kind": "webauthn",
        "principal": "staff",
        "handle": handle,
        "team": TEAM,
        "credential_id": b64(f"credential-{handle}-0001".encode("ascii")),
        "public_key": b64(point),
        "scopes": ["record:read", "record:write", "record:security"],
    }


def rule_entries() -> list[dict[str, Any]]:
    return [
        *key_entries(),
        _agent(UNA, "una", FULL, revoked=NOW + 10**6),
        _agent(VIC, "vic", FULL, revoked=REVOKED),
        _agent(WES, "wes", FULL, revoked=REVOKED),
        _staff("wes-passkey-1", "wes", 4301),
        _agent(XIA, "xia", FULL, revoked=REVOKED),
        _agent(XIA_WITHOUT_SECURITY, "xia", ["record:read", "record:write", "device:controller"], device="laptop-2"),
        _agent(YAN, "yan", FULL, revoked=REVOKED),
        _agent(YAN_WITHOUT_WRITE, "yan", ["record:read", "record:security", "device:executor"], device="laptop-2"),
        _agent(RITA, "rita", ["record:read", "record:security", "device:executor"]),
        _staff("erin-passkey-1", "erin", 4302),
        _agent(REX, "rex", FULL, revoked=NOW - 60),
        _agent(LONG, "long", ["record:read", "record:write", "device:controller"], team=LONG_TEAM),
        {
            "id": PLAIN_WATCHER,
            "kind": "ed25519",
            "principal": "service",
            "handle": "plain-watcher",
            "team": TEAM,
            "device": "runner-70",
            "public_key": public_text(EXTRA_SEEDS[PLAIN_WATCHER]),
            "scopes": ["record:read", "record:watch"],
            "subjects": [REPO],
            "expires": fmt(NOW + 365 * 86400),
        },
    ]


def write_rule_files(directory: Path) -> tuple[Path, Path]:
    teams = directory / "rule-teams.yaml"
    members = ["alice", "bob", "carol", "una", "vic", "wes", "xia", "yan", "rita", "erin", "rex"]
    teams.write_text(
        yaml.safe_dump(
            {"teams": {TEAM: {"members": members}, OTHER_TEAM: {"members": ["dave"]}, LONG_TEAM: {"members": ["long"]}}}
        ),
        encoding="utf-8",
    )
    keys = directory / "rule-keys.yaml"
    keys.write_text(yaml.safe_dump({"keys": rule_entries()}, sort_keys=False), encoding="utf-8")
    return keys, teams


@pytest.fixture
def hub(record_db: RecordDatabase, cfg: Config, tmp_path: Path) -> Iterator[Hub]:
    migrated(record_db)
    made = start_hub(record_db, cfg, tmp_path, keys_and_teams=write_rule_files(tmp_path), seeds=RULE_SEEDS)
    try:
        yield made
    finally:
        made.close()


Changes = Sequence[tuple[str, "str | None", "str | None"]]


def no(status: int, value: str) -> tuple[int, bytes]:
    """A refusal of section 6.7: its status and its exact body."""
    return status, error(value)


def refused(hub: Hub, key_id: str, object_id: str, changes: Changes, path: str = EVENTS, object_type: str = "task") -> tuple[int, bytes]:
    return hub.refused(key_id, object_type, object_id, changes, path)


def accept(hub: Hub, key_id: str, object_id: str, changes: Changes, path: str = EVENTS, object_type: str = "task") -> None:
    hub.write(key_id, object_type, object_id, changes, path)


def shown(hub: Hub, object_id: str) -> dict[str, Any]:
    """The object as alice reads it (section 7.5); alice sees every task of the team `example`."""
    return hub.signed(ALICE, f"{OBJECT}?id={object_id}", 200).json()["object"]


def create_work(hub: Hub, *extra: tuple[str, str | None, str | None], key_id: str = ALICE) -> str:
    object_id = new_id()
    accept(hub, key_id, object_id, [*task_create(), *extra])
    return object_id


def create_security(hub: Hub, short_name: str, *extra: tuple[str, str | None, str | None]) -> str:
    object_id = new_id()
    accept(hub, ALICE, object_id, [*security_create(short_name), *extra])
    return object_id


def at_status(hub: Hub, status: str, *, links: bool = True, completion: str | None = None) -> str:
    """A task of kind `work` assigned to alice, brought to `status` by the rows of section 6.4:
    `in_progress` by row 2, `ready_for_review` by row 3, `dropped` by row 6, `done` by the watcher
    (row 8, with links under `completion` `merged`) and `merged` by the watcher (row 9, with links
    under `completion` `verified`)."""
    extra: list[tuple[str, str | None, str | None]] = [("assignee", None, "alice")]
    if links:
        extra.append(("links", None, LINK))
    if completion is not None:
        extra.append(("completion", None, completion))
    object_id = create_work(hub, *extra)
    steps = {
        "open": [],
        "in_progress": [(ALICE, "open", "in_progress", EVENTS)],
        "ready_for_review": [(ALICE, "open", "in_progress", EVENTS), (ALICE, "in_progress", "ready_for_review", EVENTS)],
        "dropped": [(ALICE, "open", "dropped", EVENTS)],
        "done": [(WATCHER, "open", "done", WATCH)],
        "merged": [(WATCHER, "open", "merged", WATCH)],
    }[status]
    for key_id, old, new, path in steps:
        accept(hub, key_id, object_id, [("status", old, new)], path)
    assert shown(hub, object_id)["status"] == status
    return object_id


def merged_without_links(hub: Hub) -> str:
    """A task in `merged` without `links` under `completion` `merged`: row 10 under `verified`, then
    the rule switched without a change of `status` (section 6.4, below the table)."""
    object_id = at_status(hub, "in_progress", links=False, completion="verified")
    accept(hub, ALICE, object_id, [("status", "in_progress", "merged")])
    accept(hub, ALICE, object_id, [("completion", "verified", "merged")])
    assert (shown(hub, object_id)["status"], shown(hub, object_id)["completion"]) == ("merged", "merged")
    return object_id


# --- section 6.4: each row accepted, every unreachable target, and the column "Who" -------------------


def test_each_row_of_section_6_4_is_accepted_through_the_endpoint(hub: Hub) -> None:
    # Rows 1 and 2 at create.
    opened = create_work(hub)
    assert shown(hub, opened)["status"] == "open"
    started = create_work(hub, ("assignee", None, "alice"), ("status", None, "in_progress"))
    assert shown(hub, started)["status"] == "in_progress"
    # Row 2 from `open`, then rows 3, 4, 5, 6 and 7.
    for changes, status in (
        ([("assignee", None, "alice"), ("status", "open", "in_progress")], "in_progress"),
        ([("status", "in_progress", "ready_for_review")], "ready_for_review"),
        ([("status", "ready_for_review", "in_progress")], "in_progress"),
        ([("status", "in_progress", "open")], "open"),
        ([("status", "open", "dropped")], "dropped"),
        ([("status", "dropped", "open")], "open"),
    ):
        accept(hub, BOB, opened, changes)
        assert shown(hub, opened)["status"] == status
    # Rows 8 and 9, by the watcher.
    at_status(hub, "done")
    at_status(hub, "merged", completion="verified")
    # Row 10 under `merged` and under `verified`, by the assignee.
    plain = at_status(hub, "in_progress", links=False)
    accept(hub, ALICE, plain, [("status", "in_progress", "done")])
    assert shown(hub, plain)["status"] == "done"
    # Row 12, by the assignee, on a task in `merged` without links.
    ruled = merged_without_links(hub)
    accept(hub, ALICE, ruled, [("status", "merged", "done")])
    assert (shown(hub, ruled)["status"], shown(hub, ruled)["completed_links"]) == ("done", [])
    # Row 13 and then row 14, by a writer who is not the assignee.
    linked = at_status(hub, "merged", completion="verified")
    accept(hub, BOB, linked, [("completion", "verified", "merged"), ("status", "merged", "done")])
    assert (shown(hub, linked)["status"], shown(hub, linked)["completed_links"]) == ("done", [])
    accept(hub, BOB, linked, [("status", "done", "in_progress")])
    assert shown(hub, linked)["status"] == "in_progress"


# For each status, every target that no row of section 6.4 reaches from it by its state conditions;
# `in_progress` and `ready_for_review` reach every other status.
UNREACHED = [
    ("open", "ready_for_review"),
    ("merged", "open"),
    ("merged", "ready_for_review"),
    ("merged", "dropped"),
    ("done", "open"),
    ("done", "ready_for_review"),
    ("done", "merged"),
    ("done", "dropped"),
    ("dropped", "in_progress"),
    ("dropped", "ready_for_review"),
    ("dropped", "merged"),
    ("dropped", "done"),
]


@pytest.mark.parametrize(("status", "target"), UNREACHED)
def test_a_target_that_no_row_reaches_gets_transition_invalid(hub: Hub, status: str, target: str) -> None:
    object_id = at_status(hub, status, completion="verified" if status == "merged" else None)
    assert refused(hub, ALICE, object_id, [("status", status, target)]) == no(409, "transition_invalid")


@pytest.mark.parametrize("target", ["ready_for_review", "merged", "done", "dropped"])
def test_a_create_in_a_status_that_no_row_reaches_gets_transition_invalid(hub: Hub, target: str) -> None:
    changes = [*task_create(), ("assignee", None, "alice"), ("links", None, LINK), ("status", None, target)]
    assert refused(hub, ALICE, new_id(), changes) == no(409, "transition_invalid")


def test_the_column_who_refuses_every_other_actor_that_reaches_check_18(hub: Hub) -> None:
    not_allowed = no(403, "change_not_allowed")
    # The writer rows 3, 4, 5, 6, 7 and 14 through the watch endpoint: the watcher is no writer.
    for status, target, completion in (
        ("in_progress", "ready_for_review", None),
        ("ready_for_review", "in_progress", None),
        ("in_progress", "open", None),
        ("open", "dropped", None),
        ("dropped", "open", None),
        ("done", "in_progress", None),
        ("merged", "in_progress", "verified"),
    ):
        object_id = at_status(hub, status, completion=completion)
        assert refused(hub, WATCHER, object_id, [("status", status, target)], WATCH) == not_allowed
    # Row 13 through the watch endpoint names `completion`: check 12 (e) refuses it first.
    merged = at_status(hub, "merged", completion="verified")
    switch = [("completion", "verified", "merged"), ("status", "merged", "done")]
    assert refused(hub, WATCHER, merged, switch, WATCH) == not_allowed
    # Rows 1 and 2 by the watcher: check 12 (a) refuses a create first.
    assert refused(hub, WATCHER, new_id(), [*task_create(), ("links", None, LINK)], WATCH) == no(404, "object_not_found")
    # A key without `record:write` gets `scope_missing` at check 9 of KEY-PROTOCOL.md section 5.
    assert refused(hub, RITA, new_id(), task_create()) == no(403, "scope_missing")
    # Row 2 for kind `security`: a writer whose handle is not the assignee after the event.
    issue = create_security(hub, "login-bypass", ("assignee", None, "alice"))
    assert refused(hub, UNA, issue, [("status", "open", "in_progress")]) == not_allowed
    # The watcher rows 8 and 9 by a writer.
    for completion, target in (("merged", "done"), ("verified", "merged")):
        object_id = at_status(hub, "open", completion=completion)
        assert refused(hub, ALICE, object_id, [("status", "open", target)]) == not_allowed
    # The assignee rows 10 and 12 by a writer who is not the assignee, and row 10 by the assignee
    # in an event that changes `assignee` (section 6.4: then the actor is no assignee).
    plain = at_status(hub, "in_progress", links=False)
    assert refused(hub, BOB, plain, [("status", "in_progress", "done")]) == not_allowed
    handed = [("assignee", "alice", "bob"), ("status", "in_progress", "done")]
    assert refused(hub, ALICE, plain, handed) == not_allowed
    ruled = merged_without_links(hub)
    assert refused(hub, BOB, ruled, [("status", "merged", "done")]) == not_allowed
    # Row 11 by an `agent` key: `verification` written by an agent.
    confirm = [("status", "merged", "done"), ("verification", None, "Checked in production")]
    assert refused(hub, ALICE, merged, confirm) == not_allowed


# --- who may edit a task (section 6.5) -----------------------------------------------------------------


def test_a_writer_with_record_security_edits_a_security_issue_assigned_to_another_handle(hub: Hub) -> None:
    issue = create_security(hub, "login-bypass", ("links", None, LINK))
    accept(hub, UNA, issue, [("assignee", None, "una"), ("status", "open", "in_progress")])
    for old, new in (("in_progress", "ready_for_review"), ("ready_for_review", "open"), ("open", "dropped"), ("dropped", "open")):
        accept(hub, ALICE, issue, [("status", old, new)])
    accept(hub, ALICE, issue, [("severity", "high", "low"), ("title", "Login bypass", "Login bypass found"), ("urgency", "8", "9")])
    accept(hub, WATCHER, issue, [("status", "open", "merged")], WATCH)
    accept(hub, ALICE, issue, [("status", "merged", "in_progress")])  # row 14
    accept(hub, WATCHER, issue, [("status", "in_progress", "merged")], WATCH)
    accept(hub, ALICE, issue, [("completion", "verified", "merged"), ("fix", None, LINK), ("status", "merged", "done")])  # row 13
    task = shown(hub, issue)
    assert (task["status"], task["completion"], task["assignee"], task["severity"], task["urgency"], task["title"]) == (
        "done", "merged", "una", "low", 9, "Login bypass found",
    )
    # Limited to the assignee: a change of `assignee` while una can act, and row 10.
    assert refused(hub, ALICE, issue, [("assignee", "una", "alice")]) == no(403, "change_not_allowed")
    accept(hub, ALICE, issue, [("status", "done", "in_progress")])
    assert refused(hub, ALICE, issue, [("status", "in_progress", "done")]) == no(403, "change_not_allowed")


def test_the_defaults_of_status_and_completion_per_kind_and_a_create_in_merged_or_done(hub: Hub) -> None:
    work = create_work(hub)
    issue = create_security(hub, "login-bypass")
    assert (shown(hub, work)["status"], shown(hub, work)["completion"]) == ("open", "merged")
    assert (shown(hub, issue)["status"], shown(hub, issue)["completion"]) == ("open", "verified")
    for status in ("merged", "done"):
        changes = [*task_create(), ("links", None, LINK), ("status", None, status)]
        assert refused(hub, ALICE, new_id(), changes) == no(409, "transition_invalid")


def test_row_10_from_open_needs_row_2_first_and_row_8_matches_a_task_with_links(hub: Hub) -> None:
    plain = create_work(hub, ("assignee", None, "alice"))
    assert refused(hub, ALICE, plain, [("status", "open", "done")]) == no(409, "transition_invalid")
    accept(hub, ALICE, plain, [("status", "open", "in_progress")])
    accept(hub, ALICE, plain, [("status", "in_progress", "done")])
    assert shown(hub, plain)["status"] == "done"
    linked = create_work(hub, ("assignee", None, "alice"), ("links", None, LINK))
    assert refused(hub, ALICE, linked, [("status", "open", "done")]) == no(403, "change_not_allowed")


def test_a_security_issue_is_started_and_handed_over_only_by_its_own_assignee(hub: Hub) -> None:
    issue = create_security(hub, "login-bypass")
    accept(hub, UNA, issue, [("assignee", None, "una"), ("status", "open", "in_progress")])
    other = create_security(hub, "other-bypass")
    started_for_another = [("assignee", None, "una"), ("status", "open", "in_progress")]
    assert refused(hub, ALICE, other, started_for_another) == no(403, "change_not_allowed")
    assert refused(hub, ALICE, issue, [("assignee", "una", "alice")]) == no(403, "change_not_allowed")
    accept(hub, UNA, issue, [("assignee", "una", "alice")])
    assert shown(hub, issue)["assignee"] == "alice"


# --- actor rules 4 (c) and 5 for kind `security` (section 6.5) ----------------------------------------


def taken_by(hub: Hub, key_id: str, handle: str, short_name: str) -> str:
    """A security issue that the key `key_id` took by setting `assignee` to its own handle."""
    issue = create_security(hub, short_name)
    accept(hub, key_id, issue, [("assignee", None, handle)])
    return issue


def test_rule_4_case_c_takes_over_an_orphaned_assignee_and_nothing_else(hub: Hub) -> None:
    by_vic = taken_by(hub, VIC, "vic", "issue-vic")
    by_una = taken_by(hub, UNA, "una", "issue-una")
    by_wes = taken_by(hub, WES, "wes", "issue-wes")
    by_xia = taken_by(hub, XIA, "xia", "issue-xia")
    by_yan = taken_by(hub, YAN, "yan", "issue-yan")
    # The full keys of vic, wes, xia and yan are revoked at REVOKED; una's only long after.
    hub.clock.now = AFTER_REVOCATION
    # Every agent key of vic is revoked: alice takes over, but never for a third handle.
    assert refused(hub, ALICE, by_vic, [("assignee", "vic", "una")]) == no(403, "change_not_allowed")
    accept(hub, ALICE, by_vic, [("assignee", "vic", "alice")])
    # una keeps a key whose `revoked` time lies after `received_at`: not orphaned.
    assert refused(hub, ALICE, by_una, [("assignee", "una", "alice")]) == no(403, "change_not_allowed")
    # wes keeps only a `staff` entry, xia only a key without `record:security`, yan only a key
    # without `record:write`: each is orphaned. A writer without `record:security` does not see the
    # issue.
    assert refused(hub, BOB, by_yan, [("assignee", "yan", "bob")]) == no(404, "object_not_found")
    for issue, handle in ((by_wes, "wes"), (by_xia, "xia"), (by_yan, "yan")):
        accept(hub, ALICE, issue, [("assignee", handle, "alice")])
        assert shown(hub, issue)["assignee"] == "alice"


def test_rule_5_a_security_issue_is_handed_over_only_to_a_handle_that_can_act_on_it(hub: Hub) -> None:
    issue = create_security(hub, "login-bypass", ("assignee", None, "alice"))
    # bob's only key lacks `record:security`, rita's lacks `record:write`, erin has only a `staff`
    # entry.
    for handle in ("bob", "rita", "erin"):
        assert refused(hub, ALICE, issue, [("assignee", "alice", handle)]) == no(409, "assignee_invalid")
    accept(hub, ALICE, issue, [("assignee", "alice", "una")])
    assert shown(hub, issue)["assignee"] == "una"


def test_assignee_invalid_on_a_task_of_kind_work_and_a_staff_handle_accepted(hub: Hub) -> None:
    work = create_work(hub)
    # An unknown handle, a handle of another team, a handle whose keys are all revoked, and a
    # `service` handle.
    for handle in ("nobody", "dave", "rex", "example-watcher"):
        assert refused(hub, ALICE, work, [("assignee", None, handle)]) == no(409, "assignee_invalid")
    accept(hub, ALICE, work, [("assignee", None, "erin")])
    assert shown(hub, work)["assignee"] == "erin"


# --- check 12 of section 6.7 ------------------------------------------------------------------------


def test_check_12_b_a_create_without_kind_or_task_and_the_watch_endpoint_first(hub: Hub) -> None:
    assert refused(hub, ALICE, new_id(), [("team", None, TEAM), ("title", None, "Fix login")]) == no(409, "field_required")
    assert refused(hub, ALICE, new_id(), [("body", None, "Seen")], object_type="comment") == no(409, "field_required")
    # Through the watch endpoint an object that does not exist is refused at check 12 (a) first.
    assert refused(hub, WATCHER, new_id(), [("team", None, TEAM), ("title", None, "Fix login")], WATCH) == no(
        404, "object_not_found"
    )


def test_check_12_c_a_create_for_another_team_never_reaches_short_name_taken(hub: Hub) -> None:
    assert refused(hub, ALICE, new_id(), task_create(team=OTHER_TEAM)) == no(403, "change_not_allowed")
    ops_issue = [
        ("kind", None, "security"),
        ("repository", None, REPO),
        ("severity", None, "high"),
        ("short_name", None, "ops-bypass"),
        ("team", None, OTHER_TEAM),
        ("title", None, "Ops bypass"),
        ("urgency", None, "5"),
    ]
    accept(hub, DAVE, new_id(), ops_issue)
    assert refused(hub, ALICE, new_id(), ops_issue) == no(403, "change_not_allowed")
    assert refused(hub, ALICE, new_id(), [("kind", None, "work"), ("title", None, "Fix login")]) == no(409, "field_required")


def test_check_12_c_a_team_of_100_bytes_and_one_of_101_bytes(hub: Hub) -> None:
    accept(hub, LONG, new_id(), task_create(team=LONG_TEAM))
    assert refused(hub, ALICE, new_id(), task_create(team="t" * 101)) == no(403, "change_not_allowed")


def test_check_12_d_a_comment_task_that_is_no_uuid_and_a_create_on_an_unseen_object(hub: Hub) -> None:
    comment = [("body", None, "Seen"), ("task", None, "not-a-uuid")]
    assert refused(hub, ALICE, new_id(), comment, object_type="comment") == no(404, "object_not_found")
    hidden = new_id()
    accept(hub, DAVE, hidden, task_create(team=OTHER_TEAM))
    assert refused(hub, ALICE, hidden, task_create()) == no(404, "object_not_found")
    on_hidden = [("body", None, "Seen"), ("task", None, hidden)]
    assert refused(hub, ALICE, new_id(), on_hidden, object_type="comment") == no(404, "object_not_found")


def test_check_14_examines_the_old_value_of_urgency(hub: Hub) -> None:
    issue = create_security(hub, "login-bypass")
    assert refused(hub, ALICE, issue, [("urgency", "07", "9")]) == no(400, "value_invalid")


# --- rows 13 and 14 and `completed_links` -----------------------------------------------------------


def test_row_13_needs_done_in_the_same_event_and_a_rule_change_on_a_done_task_keeps_done(hub: Hub) -> None:
    linked = at_status(hub, "merged", completion="verified")
    assert refused(hub, ALICE, linked, [("completion", "verified", "merged")]) == no(409, "transition_invalid")
    accept(hub, ALICE, linked, [("completion", "verified", "merged"), ("status", "merged", "done")])
    accept(hub, ALICE, linked, [("completion", "merged", "verified")])
    task = shown(hub, linked)
    assert (task["status"], task["completion"], task["completed_links"]) == ("done", "verified", [])


def test_row_14_sets_completed_links_and_row_10_compares_links_with_them(hub: Hub) -> None:
    done = at_status(hub, "done")
    accept(hub, ALICE, done, [("status", "done", "in_progress")])
    assert shown(hub, done)["completed_links"] == [LINK]
    # Row 10 with `links` equal to `completed_links`.
    accept(hub, ALICE, done, [("status", "in_progress", "done")])
    assert shown(hub, done)["status"] == "done"
    # After a change of `links` row 10 no longer matches; row 8 does, and it is the watcher's.
    accept(hub, ALICE, done, [("status", "done", "in_progress")])
    accept(hub, ALICE, done, [("links", LINK, f"{LINK} {OTHER_LINK}")])
    assert refused(hub, ALICE, done, [("status", "in_progress", "done")]) == no(403, "change_not_allowed")
    # Row 14 together with the switch of `completion` from `verified` to `merged`.
    merged = at_status(hub, "merged", completion="verified")
    accept(hub, BOB, merged, [("completion", "verified", "merged"), ("status", "merged", "in_progress")])
    task = shown(hub, merged)
    assert (task["status"], task["completion"], task["completed_links"]) == ("in_progress", "merged", [LINK])


def test_the_four_cases_of_which_state_a_condition_reads(hub: Hub) -> None:
    plain = create_work(hub, ("assignee", None, "alice"))
    sets_links = [("links", None, LINK), ("status", "open", "done")]
    assert refused(hub, ALICE, plain, sets_links) == no(409, "transition_invalid")
    linked = create_work(hub, ("assignee", None, "alice"), ("links", None, LINK))
    removes_links = [("links", LINK, None), ("status", "open", "done")]
    assert refused(hub, ALICE, linked, removes_links) == no(409, "links_frozen")
    ruled = merged_without_links(hub)
    assert refused(hub, ALICE, ruled, [("links", None, LINK), ("status", "merged", "done")]) == no(409, "links_frozen")
    merged = at_status(hub, "merged", completion="verified")
    switch = [("completion", "verified", "merged"), ("links", LINK, None), ("status", "merged", "done")]
    assert refused(hub, ALICE, merged, switch) == no(409, "links_frozen")


# --- section 6.6 ------------------------------------------------------------------------------------


def test_links_frozen_in_merged_and_done(hub: Hub) -> None:
    for object_id in (at_status(hub, "merged", completion="verified"), at_status(hub, "done")):
        assert refused(hub, ALICE, object_id, [("links", LINK, f"{LINK} {OTHER_LINK}")]) == no(409, "links_frozen")


SECURITY_REQUIRED = ("repository", "severity", "short_name", "title", "urgency")


@pytest.mark.parametrize("missing", SECURITY_REQUIRED)
def test_field_required_for_each_required_field_of_a_security_create(hub: Hub, missing: str) -> None:
    changes = [change for change in security_create("login-bypass") if change[0] != missing]
    assert refused(hub, ALICE, new_id(), changes) == no(409, "field_required")


def test_field_required_for_a_title_in_progress_without_assignee_and_done_without_fix(hub: Hub) -> None:
    assert refused(hub, ALICE, new_id(), [("kind", None, "work"), ("team", None, TEAM)]) == no(409, "field_required")
    work = create_work(hub)
    assert refused(hub, ALICE, work, [("title", "Fix login", None)]) == no(409, "field_required")
    started = at_status(hub, "in_progress")
    assert refused(hub, ALICE, started, [("assignee", "alice", None)]) == no(409, "field_required")
    issue = create_security(hub, "login-bypass", ("links", None, LINK))
    accept(hub, WATCHER, issue, [("status", "open", "merged")], WATCH)
    switch = [("completion", "verified", "merged"), ("status", "merged", "done")]
    assert refused(hub, ALICE, issue, switch) == no(409, "field_required")


@pytest.mark.parametrize(
    ("name", "value"),
    [("repository", REPO), ("short_name", "login-bypass"), ("severity", "high"), ("urgency", "8"), ("source", "scanner-1"), ("fix", LINK)],
)
def test_field_not_allowed_for_each_security_field_on_kind_work(hub: Hub, name: str, value: str) -> None:
    work = create_work(hub)
    assert refused(hub, ALICE, work, [(name, None, value)]) == no(400, "field_not_allowed")


def test_field_immutable_state_mismatch_and_the_fields_of_a_comment(hub: Hub) -> None:
    work = create_work(hub)
    other = create_work(hub)
    assert refused(hub, ALICE, work, [("kind", "work", "security")]) == no(409, "field_immutable")
    assert refused(hub, ALICE, work, [("team", TEAM, OTHER_TEAM)]) == no(409, "field_immutable")
    comment = new_id()
    accept(hub, ALICE, comment, [("body", None, "Seen"), ("task", None, work)], object_type="comment")
    assert refused(hub, ALICE, comment, [("body", "Seen", "Changed")], object_type="comment") == no(409, "field_immutable")
    assert refused(hub, ALICE, comment, [("task", work, other)], object_type="comment") == no(409, "field_immutable")
    assert refused(hub, ALICE, comment, [("title", None, "Fix login")], object_type="comment") == no(
        400, "field_not_allowed"
    )
    # `state_mismatch`: a stale `old`, a create with an `old` value, and another `object_type`.
    assert refused(hub, ALICE, work, [("title", "Stale", "New")]) == no(409, "state_mismatch")
    with_old = [("kind", None, "work"), ("team", None, TEAM), ("title", "Old", "Fix login")]
    assert refused(hub, ALICE, new_id(), with_old) == no(409, "state_mismatch")
    assert refused(hub, ALICE, work, [("body", None, "Seen")], object_type="comment") == no(409, "state_mismatch")
    assert refused(hub, ALICE, comment, [("title", None, "Fix login")]) == no(409, "state_mismatch")


def test_check_16_a_change_of_kind_together_with_a_field_the_kind_does_not_have(hub: Hub) -> None:
    work = create_work(hub)
    changes = [("kind", "work", "security"), ("repository", None, REPO)]
    assert refused(hub, ALICE, work, changes) == no(400, "field_not_allowed")


def test_short_name_taken_in_the_same_team_and_free_in_another(hub: Hub) -> None:
    create_security(hub, "login-bypass")
    assert refused(hub, ALICE, new_id(), security_create("login-bypass")) == no(409, "short_name_taken")
    ops_issue = [
        ("kind", None, "security"),
        ("repository", None, REPO),
        ("severity", None, "high"),
        ("short_name", None, "login-bypass"),
        ("team", None, OTHER_TEAM),
        ("title", None, "Login bypass"),
        ("urgency", None, "8"),
    ]
    accept(hub, DAVE, new_id(), ops_issue)


def test_verification_outside_row_11_gets_transition_invalid(hub: Hub) -> None:
    # Section 14 also asks for a `verification` removed (`new` `-`) on a task that holds a value.
    # Only row 11 sets a value and only a staff member writes row 11, whose keys sign no event in
    # this step, so no task holds a value through the endpoint; the fold directly carries that case.
    work = create_work(hub)
    assert refused(hub, ALICE, work, [("verification", None, "Checked in production")]) == no(409, "transition_invalid")
    created = [*task_create(), ("verification", None, "Checked in production")]
    assert refused(hub, ALICE, new_id(), created) == no(409, "transition_invalid")


# --- the watcher (section 8) ------------------------------------------------------------------------


def test_the_refusals_of_the_watcher_that_need_a_security_issue_or_a_comment(hub: Hub) -> None:
    issue = create_security(hub, "login-bypass", ("links", None, LINK))
    hidden = create_security(hub, "hidden-bypass", ("links", None, OTHER_LINK))
    accept(hub, ALICE, hidden, [("repository", REPO, OTHER_REPO)])
    not_allowed = no(403, "change_not_allowed")
    # `fix` to a reference that is not one of the links.
    elsewhere = [("fix", None, f"{REPO}#43"), ("status", "open", "merged")]
    assert refused(hub, WATCHER, issue, elsewhere, WATCH) == not_allowed
    # Check 12 (e): `short_name` to that of an issue the watcher does not see, and to an unused one.
    for name in ("hidden-bypass", "unused-bypass"):
        assert refused(hub, WATCHER, issue, [("short_name", "login-bypass", name)], WATCH) == not_allowed
    # An event without a change of `status`.
    assert refused(hub, WATCHER, issue, [("fix", None, LINK)], WATCH) == not_allowed
    # The id of an existing comment, and a task that does not exist.
    comment = new_id()
    accept(hub, ALICE, comment, [("body", None, "Seen"), ("task", None, issue)], object_type="comment")
    missing = no(404, "object_not_found")
    assert refused(hub, WATCHER, comment, [("status", "open", "merged")], WATCH) == missing
    assert refused(hub, WATCHER, new_id(), [("status", "open", "merged")], WATCH) == missing
    # A watcher without `record:security` whose subjects hold the repository and the links.
    assert refused(hub, PLAIN_WATCHER, issue, [("status", "open", "merged")], WATCH) == missing
    accept(hub, WATCHER, issue, [("fix", None, LINK), ("status", "open", "merged")], WATCH)


# --- the fold of an exported record (section 14, the last case of "The reducer") ---------------------


def test_the_fold_of_an_exported_record_gives_the_same_state_as_the_hub(
    hub: Hub, record_db: RecordDatabase, tmp_path: Path
) -> None:
    at_status(hub, "done")
    at_status(hub, "merged", completion="verified")
    issue = create_security(hub, "login-bypass", ("links", None, LINK))
    accept(hub, ALICE, new_id(), [("body", None, "Seen"), ("task", None, issue)], object_type="comment")
    accept(hub, DAVE, new_id(), task_create(team=OTHER_TEAM))
    out = tmp_path / "export.jsonl"
    exported = commands.run_export(["--out", str(out)], {"AGENT_CORE_RECORD_DATABASE": record_db.writer_url}, lambda: NOW)
    head = hub.runtime.memory.head()
    assert head is not None
    assert exported == (f"exported {head.position} {head.record_hash}", 0)
    data = parse_export(out.read_bytes())
    folded = verify_record(data.events, data.checkpoints, hub.runtime.keys, {}, None)
    assert folded.failure is None
    assert snapshot(folded.state) == snapshot(hub.runtime.memory.state)
