"""`register` (11-agent-access.md sections 5.2, 8.1, 8.4): a new account through an invitation with this agent paired.
The invitation link comes from stdin or the environment, is checked at the hub, opened with the pairing and the
proposed display name, and never printed."""

from __future__ import annotations

import html
import json
import re
import secrets
import stat
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from agent_platform import pairing
from agent_platform.errors import PlatformError
from agent_runner.crypto import invite as invite_format

from .conftest import Tool, browser_confirms, post_form, wait_for
from .hubfake import HubFake


def platform_invite(hub: HubFake, *, registered: bool = False) -> tuple[str, str]:
    """A platform invitation known to the hub: its link and its code text."""
    code = secrets.token_bytes(16)
    hub.invites[invite_format.invite_secrets(code).invite_id] = {"scope": "platform", "registered": registered}
    text = invite_format.format_code(code)
    return f"{hub.origin}/invite#{text}", text


def start_register(tool: Tool, link: str, name: str = " Ada Lovelace ") -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def target() -> None:
        code, document = tool.run(
            "register", "--display-name", name, "--name", "Claude Code", "--host", "mac", stdin=link
        )
        result.update(document, rc=code)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def opened_redirect(tool: Tool) -> tuple[Path, str]:
    """The redirect file the browser was given (never the link itself) and the link it forwards to."""
    opened = wait_for(lambda: tool.opened() and tool.opened()[0])
    assert opened.startswith("file://")
    path = Path(unquote(urlsplit(opened).path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    found = re.search(r'content="0;url=([^"]+)"', path.read_text())
    assert found is not None
    return path, html.unescape(found.group(1))


def test_register_opens_the_invitation_with_the_pairing_and_name(tool: Tool, hub: HubFake) -> None:
    link, text = platform_invite(hub)
    thread, result = start_register(tool, link)
    redirect, url = opened_redirect(tool)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == hub.origin
    assert parts.path == "/invite"
    code_text, rest = parts.fragment.split("&", 1)
    assert code_text == text
    params = {key: values[0] for key, values in parse_qs(rest).items()}
    assert set(params) == {"p", "f", "c", "a", "h", "u"}
    assert (params["a"], params["h"], params["u"]) == ("Claude Code", "mac", "Ada Lovelace")
    user = hub.add_user(display_name="Ada Lovelace")
    status, headers = post_form(int(params["c"]), browser_confirms(hub, user, params))
    assert status == 303
    assert headers["Location"] == f"{hub.origin}/settings?agent=connected"
    thread.join(20)
    assert not redirect.exists()
    assert result["ok"] is True, result
    assert result["user_id"] == user.id
    assert result["hub"] == hub.origin
    assert result["scope"] == "platform"
    assert result["display_name"] == "Ada Lovelace"
    assert hub.agents[result["agent_id"]].state == "active"
    # the invitation code is a secret: nothing of it in the output
    assert text not in json.dumps(result)
    assert text.replace("-", "") not in json.dumps(result)


def test_register_puts_the_message_from_stdin_into_the_link(tool: Tool, hub: HubFake) -> None:
    link, _ = platform_invite(hub)
    result: dict[str, Any] = {}

    def target() -> None:
        code, document = tool.run(
            "register",
            "--display-name",
            "Ada",
            "--message",
            "-",
            stdin="Bitte <i>bestätigen</i>\n",
            env={"AGENT_PLATFORM_INVITE": link},
        )
        result.update(document, rc=code)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    _redirect, url = opened_redirect(tool)
    params = {key: values[0] for key, values in parse_qs(urlsplit(url).fragment.split("&", 1)[1]).items()}
    assert params["t"] == "Bitte <i>bestätigen</i>"
    user = hub.add_user(display_name="Ada")
    assert post_form(int(params["c"]), browser_confirms(hub, user, params))[0] == 303
    thread.join(20)
    assert result["ok"] is True, result


def test_register_reads_the_message_from_stdin_only_when_the_link_is_not_there(tool: Tool, hub: HubFake) -> None:
    link, _ = platform_invite(hub)
    code, document = tool.run("register", "--display-name", "Ada", "--message", "-", stdin=link)
    assert (code, document["error"]) == (1, "invalid_argument")
    assert hub.agents == {}


def test_register_reads_the_link_from_the_environment(tool: Tool, hub: HubFake) -> None:
    link, _ = platform_invite(hub, registered=True)
    code, document = tool.run("register", "--display-name", "Ada", env={"AGENT_PLATFORM_INVITE": link})
    assert code == 1
    assert document == {
        "ok": False,
        "error": "hub_refused",
        "next": document["next"],
        "hub_error": "invite_registered",
        "status": 409,
    }


@pytest.mark.parametrize(
    ("stdin", "name"),
    [
        ("", "Ada"),
        ("https://hub.example/connect#ABC", "Ada"),
        ("https://hub.example/invite?x=1#ABC", "Ada"),
        ("https://hub.example/invite", "Ada"),
        ("https://hub.example/invite#not-a-code", "Ada"),
        ("ftp://hub.example/invite#AAAAA", "Ada"),
        ("http://hub.example/invite#AAAAA", "Ada"),
        ("VALID", "   "),
        ("VALID", "a" * 81),
    ],
)
def test_register_refuses_bad_input(tool: Tool, hub: HubFake, stdin: str, name: str) -> None:
    if stdin == "VALID":
        stdin, _ = platform_invite(hub)
    code, document = tool.run("register", "--display-name", name, stdin=stdin)
    assert (code, document["error"]) == (1, "invalid_argument")
    assert hub.agents == {}


def test_register_reports_an_unknown_invitation(tool: Tool, hub: HubFake) -> None:
    code = invite_format.format_code(secrets.token_bytes(16))
    status, document = tool.run("register", "--display-name", "Ada", stdin=f"{hub.origin}/invite#{code}")
    assert (status, document["error"], document["hub_error"]) == (1, "hub_refused", "invite_unknown")
    assert code not in json.dumps(document)


def test_parse_invite() -> None:
    raw = secrets.token_bytes(16)
    text = invite_format.format_code(raw)
    assert pairing.parse_invite(f" https://hub.example/invite#{text}&p=1 ") == ("https://hub.example", raw)
    with pytest.raises(PlatformError):
        pairing.parse_invite(text)


def test_register_fails_with_the_claim_and_removes_the_redirect(tool: Tool, hub: HubFake) -> None:
    link, _ = platform_invite(hub)
    thread, result = start_register(tool, link, "\ufeffAda\u3000")
    redirect, url = opened_redirect(tool)
    params = {key: values[0] for key, values in parse_qs(url.split("#", 1)[1].split("&", 1)[1]).items()}
    # trimmed like the web's name field (JavaScript `trim`)
    assert params["u"] == "Ada"
    fields = browser_confirms(hub, hub.add_user(), params)
    del fields["u"]
    assert post_form(int(params["c"]), fields)[0] == 303
    thread.join(20)
    assert (result["rc"], result["error"]) == (1, "pairing_failed")
    assert not redirect.exists()
