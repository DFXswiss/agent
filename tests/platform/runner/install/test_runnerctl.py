"""U-RUN-install-runnerctl: the runner's own commands run by root as the owner of the Mac (ADR-074 sections 3 and 8):
the runner's settings, the exact `sudo -u <owner> … env -i … python -I -B -m agent_runner` argv, the `--json` document
read as data, status, goldens, enrollment with the code on the standard input of `enroll --code-file -` (never in an
argv, never in a file) and uninstall."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_runner.install import runnerctl
from agent_runner.install.release import Manifest

from .conftest import COMMIT, HUB, ROOT_KEY, VERSION, Kit, context, owner_of, refused, runner_doc, status_doc

SUDO = "/usr/bin/sudo"
CODE = "ABCDE-FGHIJ-KLMNO"


def manifest(hub: str = HUB) -> Manifest:
    return Manifest(VERSION, COMMIT, hub, ROOT_KEY, {})


def test_the_runner_settings_of_an_own_device(kit: Kit) -> None:
    payload = kit.layout.root / "Library/Application Support/DFXAgentRunner"
    # no host reserve: the rule of every device applies (05-runner.md section 1.3)
    assert runnerctl.runner_env(kit.system, owner_of(kit.layout), manifest()) == {
        "AGENT_RUNNER_HOME": str(kit.layout.root / "Users/alice/.local/share/dfx-agent-runner"),
        "AGENT_RUNNER_HUB_URL": HUB,
        "AGENT_RUNNER_PLATFORM_ROOT_KEY": ROOT_KEY,
        "AGENT_RUNNER_POOL_SIZE": "0",
        "AGENT_RUNNER_GOLDEN_TEMPLATE": str(payload / "current/golden-src/images/golden.yaml"),
        "AGENT_RUNNER_LOGIN_AUTOMATION": "off",
        "AGENT_RUNNER_VM_PREFIX": "dfx-",
    }


def test_a_loopback_hub_is_test_mode(kit: Kit) -> None:
    env = runnerctl.runner_env(kit.system, owner_of(kit.layout), manifest("http://127.0.0.1:8080"))
    assert env["AGENT_RUNNER_TEST_MODE"] == "1"
    assert env["AGENT_RUNNER_HUB_URL"] == "http://127.0.0.1:8080"


def test_the_context_names_the_runner_home_below_the_owners_home(kit: Kit) -> None:
    ctx = context(kit)
    assert ctx.home == kit.layout.root / "Users/alice/.local/share/dfx-agent-runner"
    assert ctx.as_owner(["/bin/mkdir", "-p", "x"]) == [SUDO, "-u", "alice", "-H", "--", "/bin/mkdir", "-p", "x"]


def test_commands_run_isolated_as_the_owner(kit: Kit) -> None:
    layout = kit.layout
    ctx = context(kit, {"B_VAR": "2", "A_VAR": "1 2", "PATH": "/p"})
    argv = ctx.runner(["status", "--json"])
    assert argv == [
        SUDO,
        "-u",
        "alice",
        "-H",
        "--",
        "/usr/bin/env",
        "-i",
        "A_VAR=1 2",
        "B_VAR=2",
        f"HOME={layout.root / 'Users/alice'}",
        "PATH=/p",
        str(layout.root / "Library/Application Support/DFXAgentRunner/current/python/bin/python3"),
        "-I",
        "-B",
        "-m",
        "agent_runner",
        "status",
        "--json",
    ]
    assert f"PATH={layout.runner_path()}" in context(kit).runner(["x"])


def test_call_reads_the_last_line_as_the_document(kit: Kit) -> None:
    kit.run.answer([SUDO], stdout="warning: noise\n" + runner_doc({"fact": "runner.x", "a": 1}))
    assert runnerctl.call(context(kit), ["doctor"]) == {"ok": True, "facts": [{"fact": "runner.x", "a": 1}]}
    assert kit.run.calls[0][-2:] == ["doctor", "--json"]


@pytest.mark.parametrize("stdout", ["", "  \n", "not json\n", "[1]\n", '{"ok": true}\n', '{"facts": {}}\n'])
def test_anything_but_a_document_is_a_failed_command(kit: Kit, stdout: str) -> None:
    kit.run.answer([SUDO], rc=1, stdout=stdout)
    with refused("runner_command_failed", "status"):
        runnerctl.call(context(kit), ["status"])


def test_facts_are_selected_by_name() -> None:
    document = {"facts": [{"fact": "runner.image", "n": 1}, "junk", {"fact": "runner.image.x"}, {"fact": "image"}]}
    assert runnerctl.facts(document, "image") == [{"fact": "runner.image", "n": 1}]


def test_status(kit: Kit) -> None:
    kit.run.answer(
        [SUDO],
        stdout=status_doc(revoked="key_revoked", removed="org-1", sessions=["working", "stopped", "starting", "idle"]),
    )
    assert runnerctl.status(context(kit)) == runnerctl.RunnerStatus(
        enrolled=True,
        device_id="dev-1",
        revoked="key_revoked",
        removed="org-1",
        last_connect="2026-11-24T11:00:00Z",
        busy_sessions=2,
    )
    assert kit.run.calls[0][-2:] == ["status", "--json"]


def test_the_status_of_a_device_that_is_not_enrolled(kit: Kit) -> None:
    kit.run.answer([SUDO], stdout=status_doc(enrolled=False))
    found = runnerctl.status(context(kit))
    assert (found.enrolled, found.device_id) == (False, "")
    kit.run.answer([SUDO], stdout=runner_doc())
    assert runnerctl.status(context(kit)) == runnerctl.RunnerStatus()
    assert runnerctl.RunnerStatus() == runnerctl.RunnerStatus(False, "", "no", "no", "never", 0)


def test_a_failed_status_is_refused(kit: Kit) -> None:
    kit.run.answer([SUDO], stdout=runner_doc(ok=False, error={"code": "x"}))
    with refused("runner_command_failed", "status"):
        runnerctl.status(context(kit))


def test_goldens(kit: Kit) -> None:
    kit.run.answer([SUDO], stdout=runner_doc({"fact": "runner.image", "version": "g1"}, {"fact": "runner.other"}))
    assert runnerctl.goldens(context(kit)) == [{"fact": "runner.image", "version": "g1"}]
    assert kit.run.calls[0][-3:] == ["image", "list", "--json"]
    kit.run.answer([SUDO], stdout=runner_doc(ok=False))
    with refused("runner_command_failed", "image"):
        runnerctl.goldens(context(kit))


def enroll_context(kit: Kit) -> runnerctl.Context:
    """The owner made the runner's home; the context carries the runner's settings."""
    ctx = context(kit, runnerctl.runner_env(kit.system, owner_of(kit.layout), manifest()))
    ctx.home.mkdir(parents=True)
    return ctx


def test_enrollment_hands_the_code_over_on_standard_input(kit: Kit) -> None:
    ctx = enroll_context(kit)
    fact = {"fact": "runner.enroll", "state": "active", "device_id": "dev-1"}
    kit.run.answer([SUDO], stdout=runner_doc(fact))
    assert runnerctl.enroll(ctx, CODE) == fact
    [(argv, text)] = kit.run.inputs
    assert text == CODE + "\n"
    assert kit.run.calls == [argv]
    assert argv[:5] == [SUDO, "-u", "alice", "-H", "--"]
    assert argv[argv.index("-m") + 2 :] == ["enroll", "--hub", HUB, "--code-file", "-", "--json"]
    assert all(CODE not in part for part in argv)
    assert list(ctx.home.iterdir()) == []


def test_an_enrollment_without_a_fact_returns_nothing(kit: Kit) -> None:
    ctx = enroll_context(kit)
    kit.run.answer([SUDO], stdout=runner_doc())
    assert runnerctl.enroll(ctx, CODE) == {}


@pytest.mark.parametrize(
    ("error", "name"),
    [({"code": "invite_code_used"}, "invite_code_used"), ({"code": 5}, None), ("text", None), (None, None)],
)
def test_a_refused_enrollment_names_the_runner_code(kit: Kit, error: Any, name: str | None) -> None:
    ctx = enroll_context(kit)
    kit.run.answer([SUDO], stdout=runner_doc(ok=False, error=error))
    with refused("enroll_failed", name):
        runnerctl.enroll(ctx, CODE)
    assert list(ctx.home.iterdir()) == []


@pytest.mark.parametrize("stdout", ["crash\n", "", "not json\n", '{"ok": true}\n', "[1]\n"])
def test_a_failed_enroll_command_writes_nothing(kit: Kit, stdout: str) -> None:
    ctx = enroll_context(kit)
    kit.run.answer([SUDO], rc=1, stdout=stdout)
    with refused("runner_command_failed", "enroll"):
        runnerctl.enroll(ctx, CODE)
    assert list(ctx.home.iterdir()) == []
    assert all(CODE not in part for call in kit.run.calls for part in call)


def test_enrollment_writes_nothing_below_a_linked_owner_home(kit: Kit, tmp_path: Path) -> None:
    """Root writes no file below the owner's home any more: a link on the way is never followed for a write."""
    ctx = context(kit, {"AGENT_RUNNER_HUB_URL": HUB})
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "dfx-agent-runner").mkdir(parents=True)
    ctx.home.parent.parent.mkdir(parents=True)
    ctx.home.parent.symlink_to(elsewhere)
    kit.run.answer([SUDO], stdout=runner_doc())
    assert runnerctl.enroll(ctx, CODE) == {}
    assert list((elsewhere / "dfx-agent-runner").iterdir()) == []
    assert [text for _, text in kit.run.inputs] == [CODE + "\n"]


def test_parse_reads_the_last_line(kit: Kit) -> None:
    assert runnerctl.parse("noise\n" + runner_doc({"fact": "x"}), "status") == {"ok": True, "facts": [{"fact": "x"}]}
    with refused("runner_command_failed", "status"):
        runnerctl.parse("  \n", "status")


def test_uninstall(kit: Kit) -> None:
    kit.run.answer([SUDO], stdout=runner_doc())
    runnerctl.uninstall(context(kit))
    assert kit.run.calls[0][-2:] == ["uninstall", "--json"]
    kit.run.answer([SUDO], stdout=json.dumps({"ok": False, "facts": []}))
    with refused("uninstall_failed", "runner"):
        runnerctl.uninstall(context(kit))
