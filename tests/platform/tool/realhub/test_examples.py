"""Every example of the lookup runs against the real hub (KOR-67, owner rules E-26 and E-19): what `search` and
`<command> --help` promise is what the tool and the hub accept.

Each command's `example` (`commands.Doc`, the text `search` shows) is run as written, with the identifiers of a
prepared account put in for its placeholders (`<org_id>`, `<wg_id>` …; an identifier this account has none of becomes
a well-formed unknown one), the optional parts `[…]` included, each alternative `a|b` run once and a `--wait` cut
to one second. Allowed are
success or a refusal on the merits with a `next` step; never `invalid_argument` (the tool refuses its own example),
`request_invalid` (the hub refuses what the tool sent) or `internal` (a crash). `recover` gets the recovery code of
an agent alone registered for it. The examples that cannot run here
stand in `EXCEPTIONS` with their reason; every entry names a command that exists, and the commands that change or
end the account run last (`LAST`).
"""

from __future__ import annotations

import itertools
import re
import shlex
from typing import Any

from agent_platform import cli, lookup
from agent_runner.crypto.ids import uuid7
from tests.platform.tool.conftest import Tool, make_tool

from .support import RealHub, run_ok
from .test_alone import platform_invitation

# Commands whose example does not run in this suite, with the reason and where it is covered instead.
EXCEPTIONS = {
    "pair": "opens the user's browser and waits for the passkey (test_realhub: pairing on this and another computer)",
    "register": "opens the user's browser for the passkey of a new account (test_register, E2E-49)",
    "agents confirm": "needs the link and code of a remote pairing in progress (test_realhub, E2E-50)",
    "device add": "installs the execution service on this computer with administrator rights (E2E-56, own-device.md)",
    "device remove": "removes the execution service from this computer (E2E-56, own-device.md)",
    "claude service install": "installs a login item of this computer (test_claude)",
    "claude service remove": "removes the login item of this computer (test_claude)",
    "claude watch": "runs until it is stopped (test_claude)",
    "claude login": "starts the Claude Code login, which a person approves in the browser (test_claude)",
    "claude setup-token": "starts `claude setup-token`, which a person approves in the browser (test_claude)",
    "community serve": "a server on stdin and stdout for a chat program (test_community)",
    "mcp": "a server on stdin and stdout for a chat program (test_mcp, test_realhub_mcp)",
    "install-connector": "writes the connector into the chat program's configuration of this computer (test_mcp)",
    "passkey add": "opens the user's browser for the new passkey (test_security)",
    "github connect": "reads the login of the GitHub CLI of this computer (test_identities)",
}
# Commands that change the account for the ones after them: run in this order at the end.
LAST = ("task close", "wg archive", "member remove", "org leave", "agents revoke", "recover")
REFUSED = {"invalid_argument", "internal"}
HUB_REFUSED = {"request_invalid", "invalid_argument"}
PLACEHOLDER = re.compile(r"<[^<>]*>|\$[A-Z]+|…|https://agent\.dfx\.swiss")
STDIN_TEXT = "An example text kor67."


def commands() -> dict[str, str]:
    """Every command with its example, in the order of the tree."""
    return {entry.command: entry.doc.example for entry in lookup.entries(cli.build_parser())}


# A choice in an example: `[--before X | --after Y]` or `open|closed` (words joined by `|`, not inside `<…>`).
CHOICE = re.compile(r"\[([^\[\]]*\s\|\s[^\[\]]*)\]|(?<![<\w-])[\w-]+(?:\|[\w-]+)+(?![\w>])")


def variants(example: str) -> list[str]:
    """The example with its optional parts, once with each alternative of its choices (the n-th run takes the n-th
    alternative of every choice, or its last)."""
    text = re.sub(r"\s\.\.\.(?=\]|$)", "", example)
    choices = [
        (match.group(1) or match.group(0)).split(" | " if match.group(1) else "|") for match in CHOICE.finditer(text)
    ]
    parts = CHOICE.split(text)[::2] if choices else [text]
    width = max((len(found) for found in choices), default=1)
    made = []
    for index in range(width):
        picked = [found[min(index, len(found) - 1)] for found in choices]
        joined = "".join(itertools.chain.from_iterable(zip(parts, [*picked, ""], strict=True)))
        made.append(re.sub(r"\[([^\[\]]*)\]", r"\1", joined))
    return made


def split(example: str, values: dict[str, str]) -> tuple[list[str], str]:
    """The arguments after `agent-platform` and the text on stdin, with the placeholders put in."""

    def put(match: re.Match[str]) -> str:
        return values.get(match.group(0), str(uuid7()))

    text = re.sub(r"--wait \d+", "--wait 1", PLACEHOLDER.sub(put, example))  # the wait itself is not under test
    stdin = ""
    piped = re.match(r"printf '%s' (\"[^\"]*\"|\S+) \| (.*)", text)
    if piped:
        stdin, text = shlex.split(piped.group(1))[0], piped.group(2)
    redirected = re.search(r"\s+<\s+\S+$", text)
    if redirected:
        stdin, text = stdin or STDIN_TEXT, text[: redirected.start()]
    words = shlex.split(text)
    assert words[0] == "agent-platform", example
    return words[1:], stdin


def acceptable(code: int, document: dict[str, Any]) -> bool:
    if document.get("ok") is True:
        return code == 0
    return (
        document.get("error") not in REFUSED
        and document.get("hub_error") not in HUB_REFUSED
        and bool(document.get("next"))
    )


def test_the_examples_name_commands_and_the_exceptions_exist() -> None:
    found = commands()
    assert set(EXCEPTIONS) <= set(found)
    assert set(LAST) <= set(found) - set(EXCEPTIONS)
    assert all(example.startswith(("agent-platform ", "printf '%s' ")) for example in found.values())
    assert variants("agent-platform task list --wg <wg_id> [--state open|closed]") == [
        "agent-platform task list --wg <wg_id> --state open",
        "agent-platform task list --wg <wg_id> --state closed",
    ]
    assert variants("agent-platform device add --this-computer [--wg <wg_id> ...]") == [
        "agent-platform device add --this-computer --wg <wg_id>"
    ]
    assert split("printf '%s' \"$KEY\" | agent-platform credentials set-api-key", {"$KEY": "k"}) == (
        ["credentials", "set-api-key"],
        "k",
    )
    assert split("agent-platform task create --wg <wg_id> --text -  < prompt.txt", {"<wg_id>": "w"}) == (
        ["task", "create", "--wg", "w", "--text", "-"],
        STDIN_TEXT,
    )
    assert acceptable(1, {"ok": False, "error": "not_found", "next": "x"})
    assert not acceptable(1, {"ok": False, "error": "hub_refused", "hub_error": "request_invalid", "next": "x"})
    assert not acceptable(1, {"ok": False, "error": "not_found"})


def alone(real: RealHub, name: str) -> tuple[Tool, str]:
    """An agent alone (rule E-27): registered with a platform invitation, its own name in its private organization."""
    tool = make_tool(real.root / name.lower())
    made = run_ok(tool, "register", "--own-account", "--display-name", name, stdin=platform_invitation(real))
    return tool, str(made["user_id"])


def account(real: RealHub) -> tuple[dict[str, str], Tool]:
    """An org with a workgroup, a device, a task, a whiteboard entry, an invitation and a second member, all made by
    agents without a person; a third agent alone gives its recovery code to `recover`."""
    ada, _ada_id = alone(real, "Ada")
    bob, bob_id = alone(real, "Bob")
    org = str(run_ok(ada, "org", "create", "--name", "Acme")["org_id"])
    wg = str(run_ok(ada, "wg", "create", "--org", org, "--name", "Research")["wg_id"])
    device, _seq = real.fleet.platform_device()
    real.fleet.platform.post({"type": "org_platform_access", "org": org, "granted": True})
    run_ok(ada, "device", "assign", "--wg", wg)
    task = str(run_ok(ada, "task", "create", "--wg", wg, "--text", "Fix the login bug")["task_id"])
    entry = run_ok(ada, "whiteboard", "add", "--org", org, "--wg", wg, "--text", "A note")
    invited = run_ok(ada, "org", "invite", "--org", org)
    assert run_ok(bob, "org", "join", stdin=str(invited["invite_link"]))["org_id"] == org
    second = run_ok(ada, "org", "invite", "--org", org)
    bot = make_tool(real.root / "bot")
    moved = run_ok(bot, "register", "--own-account", "--display-name", "Bot", stdin=platform_invitation(real))
    return {
        "<org_id>": org,
        "<wg_id>": wg,
        "<task_id>": task,
        "<device_id>": device.id,
        "<user_id>": bob_id,
        "<user_id|account code|invite_id>": bob_id,
        "<entry_id>": str(entry["entry_id"]),
        "<invite_id>": str(second["invite_id"]),
        "<invitation link>": str(second["invite_link"]),
        "<your user's name>": "Ada",
        "<name>": "Carol",
        "$KEY": "sk-test-" + "k" * 20,
        "$PASSWORD": "example-password",
        "…": "Also the logout",
        "<author>": bob_id,
        "recovery_code": str(moved["recovery_code"]),
        "<next_after>": "0",
        "<next_before>": "1",
        "<seq>": "0",
        "<account_code>": str(run_ok(bob, "status")["account_code"]),
        "<agent_id>": str(uuid7()),
        "<run_id>": "tr-" + "0" * 12,
        "https://agent.dfx.swiss": real.origin,
    }, ada


def test_every_example_runs_against_the_real_hub(real: RealHub) -> None:
    values, ada = account(real)
    found = commands()
    order = [name for name in found if name not in EXCEPTIONS and name not in LAST] + list(LAST)
    failures = []
    for name in order:
        for example in variants(found[name]):
            argv, stdin = split(example, values)
            if name == "recover":  # the code of an agent alone: its account moves to this computer
                stdin = values["recovery_code"]
            code, document = ada.run(*argv, stdin=stdin)
            if not acceptable(code, document):
                failures.append(f"{example}\n    -> {document}")
    assert not failures, "\n".join(failures)
