"""The operator commands `invite-bootstrap` and `invite-list` and the dispatch of
`python -m agent_core` (PASSKEY-PROTOCOL.md sections 5.3 and 13.2)."""

from __future__ import annotations

import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml

import agent_core.__main__ as hub_main
import agent_core.record.connection as connection_module
from agent_core.passkey.encoding import fingerprint
from agent_core.passkey.operator import USAGE, Streams, run_command
from agent_core.record.connection import OWNER_VARIABLE, WRITER_VARIABLE
from tests.passkey_hub import (
    NOW,
    ORIGIN,
    RECORD_DATABASE_PASSWORD,
    RECORD_DATABASE_URL,
    HubFactory,
    PasskeyHub,
    fmt,
    hubs,  # noqa: F401
)
from tests.test_signed_requests import log_lines  # noqa: F401
from tests.webauthn_authenticator import SoftAuthenticator

BOOTSTRAP = ["invite-bootstrap", "--handle", "example-admin", "--team", "example", "--seconds", "3600"]


def without_keys_invite(hubs: HubFactory) -> list[dict[str, Any]]:  # noqa: F811
    return hubs.changed("admin-passkey-1", scopes=["record:read", "record:write"])


def use_env(monkeypatch: pytest.MonkeyPatch, hub: PasskeyHub) -> None:
    for name, value in hub.env.items():
        monkeypatch.setenv(name, value)
    # The hub refuses to start when the owner's variable is set (RECORD-PROTOCOL.md section 11.4).
    monkeypatch.delenv(OWNER_VARIABLE, raising=False)


def run(hub: PasskeyHub, args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run_command(args, Streams(out=out, err=err), hub.clock)
    return code, out.getvalue(), err.getvalue()


def invite_rows(hub: PasskeyHub) -> list[dict[str, Any]]:
    return hub.rows("SELECT * FROM passkey_invite ORDER BY id")


@pytest.fixture
def bootstrap_hub(hubs: HubFactory, monkeypatch: pytest.MonkeyPatch) -> PasskeyHub:  # noqa: F811
    hub = hubs.build(without_keys_invite(hubs))
    use_env(monkeypatch, hub)
    return hub


# --- invite-bootstrap ----------------------------------------------------------------------------------


def test_invite_bootstrap_creates_an_invite_when_no_entry_holds_keys_invite(bootstrap_hub: PasskeyHub) -> None:
    code, out, err = run(bootstrap_hub, BOOTSTRAP)
    assert code == 0
    (row,) = invite_rows(bootstrap_hub)
    token = out.strip().split("#", 1)[1]
    assert out == f"{ORIGIN}/invite#{token}\n"
    assert err == f"invite bootstrap created {row['id']} example-admin example {fmt(NOW + 3600)}\n"
    assert row["created_by"] == "operator" and row["bootstrap"] == 1 and row["creation_request"] is None
    assert row["created"] == NOW and row["expires"] == NOW + 3600
    assert token not in json.dumps(row)


@pytest.mark.parametrize(
    "args",
    [
        ["invite-bootstrap", "--handle", "example-admin", "--team", "example"],
        ["invite-bootstrap", "--team", "example", "--seconds", "3600"],
        ["invite-bootstrap", "--handle", "example-admin", "--seconds", "3600"],
        [*BOOTSTRAP[:-1], "299"],
        [*BOOTSTRAP[:-1], "86401"],
        [*BOOTSTRAP[:-1], "abc"],
        [*BOOTSTRAP[:-1], "03600"],
        [*BOOTSTRAP[:-1], "-3600"],
        ["invite-bootstrap", "--handle", "Example", "--team", "example", "--seconds", "3600"],
        ["invite-bootstrap", "--handle", "helperbot", "--team", "example", "--seconds", "3600"],
        ["invite-bootstrap", "--handle", "example-admin", "--team", "nope", "--seconds", "3600"],
    ],
)
def test_invite_bootstrap_refuses_invalid_arguments_and_writes_nothing(bootstrap_hub: PasskeyHub, args: list[str]) -> None:
    code, out, err = run(bootstrap_hub, args)
    assert (code, out) == (1, "")
    assert err == "invite-bootstrap refused: the arguments break the rules of an invite\n"
    assert invite_rows(bootstrap_hub) == []


def test_invite_bootstrap_accepts_the_lifetime_bounds(bootstrap_hub: PasskeyHub, hubs: HubFactory) -> None:  # noqa: F811
    assert run(bootstrap_hub, [*BOOTSTRAP[:-1], "300"])[0] == 0
    bootstrap_hub.clock.now = NOW + 300
    assert run(bootstrap_hub, [*BOOTSTRAP[:-1], "86400"])[0] == 0


@pytest.mark.parametrize("revoked", [None, "2027-01-15T07:59:00Z"])
def test_invite_bootstrap_refuses_when_an_entry_holds_keys_invite_revoked_or_not(
    hubs: HubFactory, monkeypatch: pytest.MonkeyPatch, revoked: str | None  # noqa: F811
) -> None:
    entries = hubs.entries() if revoked is None else hubs.changed("admin-passkey-1", revoked=revoked)
    hub = hubs.build(entries)
    use_env(monkeypatch, hub)
    code, out, err = run(hub, BOOTSTRAP)
    assert (code, out) == (1, "")
    assert err == "invite-bootstrap refused: an entry of the keys file holds keys:invite\n"
    assert invite_rows(hub) == []


def test_invite_bootstrap_refuses_while_another_bootstrap_invite_is_open(bootstrap_hub: PasskeyHub) -> None:
    assert run(bootstrap_hub, BOOTSTRAP)[0] == 0
    code, out, err = run(bootstrap_hub, [*BOOTSTRAP[:2], "other-staff", *BOOTSTRAP[3:]])
    assert (code, out) == (1, "")
    assert err == "invite-bootstrap refused: another bootstrap invite is open\n"
    assert len(invite_rows(bootstrap_hub)) == 1
    bootstrap_hub.clock.now = NOW + 3600
    assert run(bootstrap_hub, BOOTSTRAP)[0] == 0


def test_invite_bootstrap_refuses_at_the_open_limit(bootstrap_hub: PasskeyHub) -> None:
    for number in range(50):
        bootstrap_hub.store.execute(
            "INSERT INTO passkey_invite (id, token_hash, handle, team, created, expires, created_by, bootstrap) "
            "VALUES (?, ?, 'example-staff', 'example', ?, ?, 'session.x', 0)",
            (f"invite.{number:032x}", f"hash-{number}", NOW, NOW + 3600),
        )
    code, out, err = run(bootstrap_hub, BOOTSTRAP)
    assert (code, out) == (1, "")
    assert err == "invite-bootstrap refused: the limit of open invites is reached\n"
    assert len(invite_rows(bootstrap_hub)) == 50


def test_invite_bootstrap_creates_one_again_after_a_keys_file_without_keys_invite(
    hubs: HubFactory, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    hub = hubs.build()
    use_env(monkeypatch, hub)
    assert run(hub, BOOTSTRAP)[0] == 1
    redeployed = hubs.build(without_keys_invite(hubs))
    use_env(monkeypatch, redeployed)
    assert run(redeployed, BOOTSTRAP)[0] == 0


def test_two_parallel_bootstrap_runs_create_exactly_one_invite(bootstrap_hub: PasskeyHub) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(bootstrap_hub, BOOTSTRAP), range(2)))
    assert sorted(code for code, _, _ in results) == [0, 1]
    assert len(invite_rows(bootstrap_hub)) == 1


# --- a bootstrap invite at the hub --------------------------------------------------------------------


def _bootstrap_body(invite_id: str, fingerprint_text: str, received: str) -> str:
    return "\n".join(
        [
            f"Adds the administrator passkey received through bootstrap invite {invite_id} to keys.yaml, with keys:invite. Before merging, example-admin compares the fingerprint below with the one shown when the passkey was created. An operator compares credential_id and public_key in this pull request with the output of invite-list on the hub host (PASSKEY-PROTOCOL.md section 5.6). Before merging, the reviewers confirm that the GitHub account with exactly the login example-admin exists and belongs to the person who created this passkey (PASSKEY-PROTOCOL.md section 5.6); without that confirmation this pull request is not merged.",
            "",
            "- Handle: example-admin",
            "- Team: example",
            "- Created by: operator (bootstrap invite, PASSKEY-PROTOCOL.md section 5.3)",
            f"- Fingerprint: {fingerprint_text}",
            f"- Received: {received}",
            "",
            "The review of this pull request is the control over who becomes administrator.",
            "",
            "While GitHub sign-in exists, every member of a team in teams.yaml is a GitHub login that reads the whole team's data. Only after the confirmation above, and only if example-admin is not yet a member of example in teams.yaml, this pull request adds the membership.",
        ]
    )


def test_a_bootstrap_invite_at_the_hub_its_log_lines_and_its_keys_file_text(
    bootstrap_hub: PasskeyHub, log_lines: list[str]  # noqa: F811
) -> None:
    _, link, _ = run(bootstrap_hub, BOOTSTRAP)
    (row,) = invite_rows(bootstrap_hub)
    opened = bootstrap_hub.open(link.strip())
    assert opened.status == 200
    bootstrap_hub.clock.now = NOW + 20
    passkey = SoftAuthenticator()
    registered = bootstrap_hub.register(passkey, opened)
    credential = passkey.credentials[0]
    fingerprint_text = fingerprint(credential.public_key)
    assert registered.json() == {"handle": "example-admin", "fingerprint": fingerprint_text}
    lines = [line.split(" ", 2)[2] for line in log_lines if " invite bootstrap " in line]
    assert lines == [
        f"invite bootstrap opened {row['id']} example-admin",
        f"invite bootstrap received {row['id']} example-admin {fingerprint_text}",
    ]
    code, out, _ = run(bootstrap_hub, ["invite-list"])
    assert code == 0
    (item,) = [json.loads(line) for line in out.splitlines()]
    assert item["created_by"] == "operator" and item["status"] == "received"
    assert item["keys_entry"].splitlines()[0] == "  - id: example-admin-passkey-1"
    assert item["keys_entry"].splitlines()[-1] == "    scopes: [record:read, record:write, keys:invite]"
    assert item["pull_request_title"] == "Add administrator passkey example-admin-passkey-1 for example-admin"
    assert item["pull_request_body"] == _bootstrap_body(row["id"], fingerprint_text, fmt(NOW + 20))
    entry = yaml.safe_load("keys:\n" + item["keys_entry"])["keys"][0]
    assert entry["scopes"] == ["record:read", "record:write", "keys:invite"]


# --- invite-list ---------------------------------------------------------------------------------------


def test_invite_list_prints_the_received_key_and_the_text_equal_to_the_websites_list(
    hubs: HubFactory, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    hub = hubs.build()
    use_env(monkeypatch, hub)
    admin = hub.sign_in(hubs.passkeys["admin"])
    first = hub.create_invite(admin)
    hub.create_invite(admin, handle="other-staff")
    opened = hub.open(first["link"])
    hub.clock.now = NOW + 5
    assert hub.register(SoftAuthenticator(), opened).status == 200
    code, out, err = run(hub, ["invite-list"])
    assert (code, err) == (0, "")
    printed = [json.loads(line) for line in out.splitlines()]
    assert printed == hub.invites(admin)
    assert printed[0]["status"] == "received" and "public_key" in printed[0]
    assert all(line == json.dumps(item, separators=(",", ":")) for line, item in zip(out.splitlines(), printed))


def test_invite_list_of_an_empty_database_prints_nothing(bootstrap_hub: PasskeyHub) -> None:
    assert run(bootstrap_hub, ["invite-list"]) == (0, "", "")


# --- the configuration or the keys file stops the hub: status 1, nothing written --------------------


@pytest.mark.parametrize("command", [BOOTSTRAP, ["invite-list"]])
def test_a_configuration_that_stops_the_hub_exits_1_and_writes_nothing(
    bootstrap_hub: PasskeyHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: list[str]
) -> None:
    database = tmp_path / "never.sqlite"
    monkeypatch.setenv("AGENT_CORE_DATABASE", str(database))
    monkeypatch.setenv("AGENT_CORE_PUBLIC_URL", "http://127.0.0.1:8787")
    code, out, err = run(bootstrap_hub, command)
    assert (code, out) == (1, "")
    assert err == "AGENT_CORE_PUBLIC_URL must have a host name, not an IP address\n"
    assert not database.exists()


@pytest.mark.parametrize("command", [BOOTSTRAP, ["invite-list"]])
def test_a_keys_file_that_stops_the_hub_exits_1_and_writes_nothing(
    bootstrap_hub: PasskeyHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: list[str]
) -> None:
    database = tmp_path / "never.sqlite"
    keys = tmp_path / "broken-keys.yaml"
    keys.write_text("keys:\n  - id: Broken\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CORE_DATABASE", str(database))
    monkeypatch.setenv("AGENT_CORE_KEYS", str(keys))
    code, out, err = run(bootstrap_hub, command)
    assert (code, out) == (1, "")
    assert err.startswith("keys entry 0")
    assert not database.exists()


def test_a_teams_file_that_stops_the_hub_exits_1(bootstrap_hub: PasskeyHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    teams = tmp_path / "broken-teams.yaml"
    teams.write_text("nothing: here\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CORE_TEAMS", str(teams))
    code, out, err = run(bootstrap_hub, ["invite-list"])
    assert (code, out) == (1, "")
    assert "teams" in err


# Step `config` of the record start (RECORD-PROTOCOL.md section 11.4) stops the hub, so it stops
# both commands; the later steps need a connection and are no rule of the configuration.
_RECORD_CONFIG_REFUSALS = [
    pytest.param({WRITER_VARIABLE: None}, f"{WRITER_VARIABLE} is not set", id="writer-missing"),
    pytest.param({WRITER_VARIABLE: ""}, f"{WRITER_VARIABLE} is not set", id="writer-empty"),
    pytest.param(
        {WRITER_VARIABLE: RECORD_DATABASE_URL.replace("localhost", "db.example")},
        f"{WRITER_VARIABLE} does not have the form of section 15",
        id="writer-form",
    ),
    pytest.param(
        {OWNER_VARIABLE: RECORD_DATABASE_URL.replace("agent_core_writer", "agent_core_owner")},
        f"{OWNER_VARIABLE} must not be set in the hub's environment",
        id="owner-set",
    ),
    pytest.param({OWNER_VARIABLE: ""}, f"{OWNER_VARIABLE} must not be set in the hub's environment", id="owner-empty"),
]


@pytest.mark.parametrize("command", [BOOTSTRAP, ["invite-list"]])
@pytest.mark.parametrize(("changes", "reason"), _RECORD_CONFIG_REFUSALS)
def test_a_record_configuration_that_stops_the_hub_exits_1_and_writes_nothing(
    bootstrap_hub: PasskeyHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: list[str],
    changes: dict[str, str | None],
    reason: str,
) -> None:
    database = tmp_path / "never.sqlite"
    monkeypatch.setenv("AGENT_CORE_DATABASE", str(database))
    for name, value in changes.items():
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    code, out, err = run(bootstrap_hub, command)
    assert (code, out, err) == (1, "", reason + "\n")
    assert RECORD_DATABASE_PASSWORD not in err
    assert not database.exists()


@pytest.mark.parametrize("command", [BOOTSTRAP, ["invite-list"]])
def test_a_libpq_below_16_stops_the_commands_as_it_stops_the_hub(
    bootstrap_hub: PasskeyHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: list[str]
) -> None:
    database = tmp_path / "never.sqlite"
    monkeypatch.setenv("AGENT_CORE_DATABASE", str(database))
    monkeypatch.setattr(connection_module, "libpq_version", lambda: 150007)
    code, out, err = run(bootstrap_hub, command)
    assert (code, out, err) == (1, "", "the libpq that psycopg uses is older than version 16\n")
    assert not database.exists()


def test_the_commands_check_the_record_configuration_after_the_keys_file_as_the_hub_start_does(
    bootstrap_hub: PasskeyHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys = tmp_path / "broken-keys.yaml"
    keys.write_text("keys:\n  - id: Broken\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CORE_KEYS", str(keys))
    monkeypatch.delenv(WRITER_VARIABLE)
    code, out, err = run(bootstrap_hub, ["invite-list"])
    assert (code, out) == (1, "")
    assert err.startswith("keys entry 0")


# --- python -m agent_core ------------------------------------------------------------------------------


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(hub_main, "run_hub", lambda: calls.append(True))
    return calls


def test_without_an_argument_the_hub_starts_as_today(started: list[bool], monkeypatch: pytest.MonkeyPatch) -> None:
    hub_main.main([])
    monkeypatch.setattr(sys, "argv", ["agent_core"])
    hub_main.main()
    assert started == [True, True]


@pytest.mark.parametrize(
    "args",
    [
        ["serve"],
        ["--help"],
        ["invite-bootstrap", "--name", "x"],
        ["invite-bootstrap", "--handle", "a", "--handle", "b", "--team", "example", "--seconds", "3600"],
        ["invite-bootstrap", "--handle"],
        ["invite-bootstrap", "--handle=example-admin", "--team", "example", "--seconds", "3600"],
        ["invite-list", "--all"],
    ],
)
def test_an_unknown_argument_or_option_and_a_repeated_option_exit_2_and_start_nothing(
    started: list[bool], capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        hub_main.main(args)
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", USAGE + "\n")
    assert started == []


def test_the_commands_run_through_main_and_never_start_the_hub(
    bootstrap_hub: PasskeyHub, started: list[bool], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        hub_main.main(["invite-list"])
    assert exited.value.code == 0
    assert capsys.readouterr().out == ""
    with pytest.raises(SystemExit) as exited:
        hub_main.main(["invite-bootstrap", "--handle", "example-admin", "--team", "example", "--seconds", "400"])
    assert exited.value.code == 0
    assert capsys.readouterr().out.startswith(f"{ORIGIN}/invite#")
    assert started == []


def test_main_with_a_configuration_that_stops_the_hub_exits_1(
    bootstrap_hub: PasskeyHub, started: list[bool], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AGENT_CORE_KEYS")
    with pytest.raises(SystemExit) as exited:
        hub_main.main(["invite-list"])
    assert exited.value.code == 1
    assert capsys.readouterr() == ("", "AGENT_CORE_KEYS is not set\n")
    assert started == []
