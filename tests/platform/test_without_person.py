"""Rule E-27 (KOR-62): an AI agent can do everything without a person. The table "Without a person" of
`docs/platform/agent-parity.md` lists every step of the agent tool that needs a person, with the way without one (or
why it lies outside the platform); this test keeps it complete, like the parity test keeps the actions complete.

A step that needs a person is
- a fixed `next` text of the tool (`errors.NEXT`, `errors.NEXT_FOR_REASON`, `diagnose.REASONS`, the `NEXT_*` texts of
  `remote` and `alone`) that names the user, a person, a browser or a passkey, or a login a person must approve
  (`setup-token`, `claude login`, the GitHub device page of `github connect` without `--token`), and
- a `done` row of the Actions table whose command opens the user's browser or needs the user's approval or such a
  login, plus the two actions that always open the browser (`connect-agent`, `register`).

Every such step has exactly one row (`next:<code>`, `next:<reason>`, `next:<module>.<NAME>`, `action:<name>`), no row
names a step that does not exist, every row has a kind (`built`, `outside`, `person`) and names at least one command
of the tool that exists, and only one step stays with a person.
"""

from __future__ import annotations

import re
from collections import Counter

from agent_platform import alone, diagnose, errors, remote, waiting
from tests.platform.test_agent_parity import command_exists, commands, rows, table

HEADING = "Without a person"
KINDS = {"built", "outside", "person"}
NEEDS_PERSON = re.compile(
    r"\b(?:users?|persons?|browser|passkey|setup-token|claude login)\b|github connect(?! --token)", re.IGNORECASE
)
ACTION_NEEDS_PERSON = re.compile(
    r"\b(?:browser|opens|approval|setup-token|claude login)\b|github connect(?! --token)", re.IGNORECASE
)
ALWAYS = {"connect-agent", "register"}  # `pair` and `register` open the user's browser


def texts() -> dict[str, str]:
    """Every fixed next-step text of the tool under its key."""
    found = {f"next:{code}": text for code, text in errors.NEXT.items()}
    found |= {f"next:{reason}": text for (_code, reason), text in errors.NEXT_FOR_REASON.items()}
    found |= {f"next:{reason}": text for reason, text in diagnose.REASONS.items()}
    for module in (remote, alone):
        name = module.__name__.rsplit(".", 1)[-1]
        found |= {f"next:{name}.{key}": value for key, value in vars(module).items() if key.startswith("NEXT")}
    found["next:waiting.CONNECT"] = waiting.CONNECT  # the next step of a held operation's card
    return found


def needed() -> set[str]:
    steps = {key for key, text in texts().items() if NEEDS_PERSON.search(text)}
    for row in rows():
        if row["state"] == "done" and (row["action"] in ALWAYS or ACTION_NEEDS_PERSON.search(row["command"])):
            steps.add(f"action:{row['action']}")
    return steps


def listed() -> list[list[str]]:
    found = [cells for cells in table(HEADING) if cells[0] != "Step"]
    assert all(len(cells) == 3 for cells in found), found
    return found


def test_every_step_that_needs_a_person_has_a_row() -> None:
    steps = [cells[0] for cells in listed()]
    assert [step for step, count in Counter(steps).items() if count > 1] == []
    assert sorted(needed() - set(steps)) == []


def test_no_row_names_a_step_that_does_not_exist() -> None:
    known = set(texts()) | {f"action:{row['action']}" for row in rows()}
    assert sorted(cells[0] for cells in listed() if cells[0] not in known) == []


def test_every_row_has_a_kind_and_existing_commands() -> None:
    for step, way, kind in listed():
        assert kind in KINDS, step
        named = commands(way)
        assert named, step
        assert [command for command in named if not command_exists(command)] == [], step


def test_only_one_step_stays_with_a_person_and_it_concerns_accounts_of_persons() -> None:
    kept = [cells for cells in listed() if cells[2] == "person"]
    assert [cells[0] for cells in kept] == ["next:not_own_account"]
    assert "idea 34" in kept[0][1]


def test_the_patterns_find_what_they_should() -> None:
    assert NEEDS_PERSON.search("ask the user")
    assert NEEDS_PERSON.search("run `claude setup-token`")
    assert NEEDS_PERSON.search("`github connect`")
    assert not NEEDS_PERSON.search("`github connect --token -`")
    assert not NEEDS_PERSON.search("Create an organization.")
    assert "next:remote.NEXT_WAITING" in texts()
    assert "next:alone.NEXT_CONFIRMED" in texts()
