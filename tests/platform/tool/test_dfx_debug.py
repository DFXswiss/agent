"""The DFX debug sign-in of the agent tool against the hub double (DBG-01, ADR-102 sections 2 and 3):
`credentials set-dfx-debug` (the signature only on standard input, the address, the API URL and the release as
options, the stored release kept without `--release`), `credentials release-dfx-debug` (only the release changes)
and the list, which shows the API URL and the released workgroups but never the address or the signature."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from agent_platform import credentials, creds

from .conftest import Tool
from .hubfake import HubFake, User

ADDRESS = "wallet-address-for-the-tool-test"
SIGNATURE = "DFXDEBUGCANARY-signature-0123"
API = "https://api.example.com/v1"
WG = "01930001-0000-7000-8000-0000000000aa"
WG2 = "01930001-0000-7000-8000-0000000000bb"
SET = ("credentials", "set-dfx-debug", "--address", ADDRESS, "--api-url")


def stored(tool: Tool) -> dict[str, Any]:
    (row,) = [row for row in tool.run("credentials", "list")[1]["credentials"] if row["kind"] == "dfx_debug"]
    return cast(dict[str, Any], row)


def test_the_sign_in_is_stored_from_stdin_and_released_to_workgroups(tool: Tool, hub: HubFake, account: User) -> None:
    code, answer = tool.run(*SET, API + "/", "--release", WG2, "--release", WG, stdin=f"{SIGNATURE}\n")
    assert code == 0, answer
    assert answer == {"ok": True, "stored": True, "version": 1, "kind": "dfx_debug", "api_url": API, "wgs": [WG, WG2]}
    row = stored(tool)
    assert (row["status"], row["api_url"], row["wgs"]) == ("verified", API, [WG, WG2])
    # a new signature without --release keeps the release
    code, answer = tool.run(*SET, API, stdin=f"renewed-{SIGNATURE}")
    assert (code, answer["version"], answer["wgs"]) == (0, 2, [WG, WG2])
    # only the release changes
    code, answer = tool.run("credentials", "release-dfx-debug", "--wg", WG)
    assert (code, answer["version"], answer["wgs"]) == (0, 3, [WG])
    assert stored(tool)["wgs"] == [WG]
    code, answer = tool.run("credentials", "release-dfx-debug", "--none")
    assert (code, answer["wgs"]) == (0, [])
    assert stored(tool)["wgs"] == []
    listed = json.dumps(tool.run("credentials", "list")[1])
    assert SIGNATURE not in listed
    assert ADDRESS not in listed
    record = json.dumps(hub.credentials[(account.id, "dfx_debug")])
    assert SIGNATURE not in record  # sealed: the hub never sees it


@pytest.mark.parametrize(
    ("argv", "stdin", "reason"),
    [
        ((*SET, API), "", "dfx_debug"),
        ((*SET, API), f"{SIGNATURE}\nmore\n", "dfx_debug"),
        ((*SET, "http://api.example.com/v1"), SIGNATURE, "dfx_debug"),
        ((*SET, API), "with space", "dfx_debug"),
        (("credentials", "set-dfx-debug", "--address", "a b", "--api-url", API), SIGNATURE, "dfx_debug"),
        ((*SET, API, "--release", "no-uuid"), SIGNATURE, None),
        (("credentials", "release-dfx-debug"), "", "wg"),
        (("credentials", "release-dfx-debug", "--none", "--wg", WG), "", "wg"),
        (("credentials", "release-dfx-debug", "--wg", WG), "", "dfx_debug_missing"),
    ],
)
def test_wrong_input_is_refused_and_never_echoed(
    tool: Tool, account: User, argv: tuple[str, ...], stdin: str, reason: str | None
) -> None:
    code, answer = tool.run(*argv, stdin=stdin)
    assert (code != 0, answer["error"]) == (True, "invalid_argument")
    if reason is not None:
        assert answer["reason"] == reason
    assert SIGNATURE not in json.dumps(answer)


def test_too_many_workgroups_are_refused(tool: Tool, account: User) -> None:
    wgs = [f"01930001-0000-7000-8000-0000000001{index:02x}" for index in range(51)]
    argv = [arg for wg in wgs for arg in ("--release", wg)]
    _code, answer = tool.run(*SET, API, *argv, stdin=SIGNATURE)
    assert (answer["error"], answer["reason"]) == ("invalid_argument", "wg")


@pytest.mark.parametrize(
    ("body", "valid"),
    [
        ({"address": ADDRESS, "signature": SIGNATURE, "api_url": API}, True),
        ({"address": ADDRESS, "signature": SIGNATURE, "api_url": API, "use": {"wgs": [WG]}}, True),
        ({"address": ADDRESS, "signature": SIGNATURE, "api_url": "https://api.example.com:8443/v1/x"}, True),
        ({"address": ADDRESS, "signature": SIGNATURE, "api_url": "https://api.example.com/v1?x=1"}, False),
        ({"address": ADDRESS, "signature": SIGNATURE, "api_url": API, "use": {"wgs": [WG, WG]}}, False),
        ({"address": ADDRESS, "signature": SIGNATURE}, False),
        ({"address": ADDRESS, "signature": SIGNATURE, "api_url": API, "token": "x"}, False),
    ],
)
def test_bodies_follow_the_contract(body: dict[str, Any], valid: bool) -> None:
    assert credentials.valid_body("dfx_debug", body) is valid


def test_the_stored_release_is_kept_only_when_there_is_one() -> None:
    body: dict[str, Any] = {"address": ADDRESS}
    creds.keep_dfx_release(body, None, None)
    assert body == {"address": ADDRESS}
    creds.keep_dfx_release(body, None, {"address": "old"})
    assert body == {"address": ADDRESS}
    creds.keep_dfx_release(body, None, {"use": {"wgs": [WG]}})
    assert body == {"address": ADDRESS, "use": {"wgs": [WG]}}
