"""`pair` (11-agent-access.md sections 5.1, 8.1, 8.2): the pairing at the hub, the loopback listener and its
attacks, the claim and the stored state."""

from __future__ import annotations

import http.client
import json
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_platform import pairing
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto.encoding import b64u_encode

from .conftest import Tool, browser_confirms, pair_user, post_form, start_pair
from .hubfake import HubFake


def test_pair_stores_state_and_keychain_item(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user(display_name="Grace")
    result = pair_user(tool, hub, user)
    assert result["user_id"] == user.id
    assert result["display_name"] == "Grace"
    assert result["hub"] == hub.origin
    assert result["link"].startswith(f"{hub.origin}/connect#p={result['agent_id']}&f=")
    assert stat.S_IMODE(tool.home.stat().st_mode) == 0o700
    state_file = tool.home / "state.json"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    state = json.loads(state_file.read_text())
    assert state["key_store"] == "keychain"
    assert state["platform_root_key"] == hub.root_text
    assert not (tool.home / "agent.key").exists()
    assert (tool.keychain / f"agent-platform__{result['agent_id']}").exists()
    assert hub.agents[result["agent_id"]].state == "active"
    # the handover secret r never reached the hub; only the claim did
    secret = state["secret"]
    assert all(secret.encode() not in body for body in hub.bodies)
    # the private key never appears on a command line of the security program
    key_text = (tool.keychain / f"agent-platform__{result['agent_id']}").read_text()
    assert key_text not in tool.security_log.read_text()


def test_pair_without_keychain_keeps_the_key_file(tool: Tool, hub: HubFake) -> None:
    (tool.root / "bin" / "security.fail").write_text("")
    pair_user(tool, hub, hub.add_user())
    state = json.loads((tool.home / "state.json").read_text())
    assert state["key_store"] == "file"
    assert stat.S_IMODE((tool.home / "agent.key").stat().st_mode) == 0o600
    (tool.root / "bin" / "security.fail").unlink()
    assert tool.run("status")[0] == 0


def test_pair_again_replaces_the_old_agent(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    first = pair_user(tool, hub, user)
    second = pair_user(tool, hub, user)
    assert first["agent_id"] != second["agent_id"]
    assert not (tool.keychain / f"agent-platform__{first['agent_id']}").exists()
    assert json.loads((tool.home / "state.json").read_text())["agent_id"] == second["agent_id"]


def test_no_browser_prints_the_link(tool: Tool, hub: HubFake, capsys: pytest.CaptureFixture[str]) -> None:
    user = hub.add_user()
    seen: list[int] = []

    def fake_listen(pair: pairing.Pairing, ready: Callable[[int], None], timeout: float | None = None) -> None:
        ready(4242)
        seen.append(1)
        raise PlatformError("pairing_timeout")

    original = pairing.listen
    pairing.listen = fake_listen  # type: ignore[assignment]
    try:
        code, document = tool.run("pair", "--hub", hub.origin, "--no-browser")
    finally:
        pairing.listen = original
    assert code == 1
    assert document["error"] == "pairing_timeout"
    assert "/connect#p=" in capsys.readouterr().err
    assert tool.opened() == []
    assert user.id not in json.dumps(document)


@pytest.mark.parametrize(
    "variant",
    ["get", "host", "foreign", "short_r", "type", "path", "dup", "empty", "bad_k", "bad_u", "extra", "binary"],
)
def test_bad_requests_do_not_end_the_wait(tool: Tool, hub: HubFake, variant: str) -> None:
    user = hub.add_user()
    run, link = start_pair(tool, hub)
    port = int(link["c"])
    fields = browser_confirms(hub, user, link)
    bad: dict[str, str] | str = dict(fields)
    kwargs: dict[str, str] = {}
    if variant == "get":
        kwargs["method"] = "GET"
    elif variant == "host":
        kwargs["host"] = f"localhost:{port}"
    elif variant == "foreign":
        bad = {**fields, "p": "01a1ee56-5e00-7242-9e24-f87629ad2977"}
    elif variant == "short_r":
        bad = {**fields, "r": b64u_encode(b"x" * 16)}
    elif variant == "type":
        kwargs["content_type"] = "text/plain"
    elif variant == "path":
        kwargs["path"] = "/agent-pairing?x=1"
    elif variant == "dup":
        bad = "p=" + fields["p"] + "&p=" + fields["p"] + "&r=" + fields["r"] + "&k=" + fields["k"]
    elif variant == "empty":
        bad = ""
    elif variant == "bad_k":
        bad = {**fields, "k": "!!"}
    elif variant == "bad_u":
        bad = {**fields, "u": "not-a-uuid"}
    elif variant == "extra":
        bad = {**fields, "z": "1"}
    else:
        bad = "p=%ff%fe"
    status, headers = post_form(port, bad, **kwargs)
    assert status == 404
    assert not any(name.lower().startswith("access-control") for name in headers)
    assert headers.get("Content-Length") == "0"
    assert run.thread.is_alive()
    status, _ = post_form(port, fields)
    assert status == 303
    assert run.join()["ok"] is True


def test_bad_content_length_is_refused(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    run, link = start_pair(tool, hub)
    port = int(link["c"])
    for length in ("x", "99999"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.putrequest("POST", "/agent-pairing", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{port}")
        connection.putheader("Content-Type", "application/x-www-form-urlencoded")
        connection.putheader("Content-Length", length)
        connection.endheaders()
        assert connection.getresponse().status == 404
        connection.close()
    assert post_form(port, browser_confirms(hub, user, link))[0] == 303
    assert run.join()["ok"] is True


def test_wrong_secret_is_refused_by_the_hub_and_the_wait_goes_on(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    run, link = start_pair(tool, hub)
    fields = browser_confirms(hub, user, link)
    wrong = {**fields, "r": b64u_encode(secrets.token_bytes(32))}
    assert post_form(int(link["c"]), wrong)[0] == 404
    assert hub.agents[link["p"]].misses == 1
    assert post_form(int(link["c"]), fields)[0] == 303
    assert run.join()["ok"] is True


def test_handover_without_user_fails(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    run, link = start_pair(tool, hub)
    fields = browser_confirms(hub, user, link)
    del fields["u"]
    status, headers = post_form(int(link["c"]), fields)
    assert status == 303
    assert headers["Location"] == f"{hub.origin}/settings?agent=failed"
    result = run.join()
    assert result["error"] == "pairing_failed"
    assert result["reason"] == "user_id_missing"
    assert not (tool.home / "state.json").exists()


def test_substituted_bundle_fails(tool: Tool, hub: HubFake) -> None:
    user, other = hub.add_user(), hub.add_user()
    run, link = start_pair(tool, hub)
    fields = browser_confirms(hub, user, link)
    # a hub that put an identity of its choosing into the row cannot seal it under k (it lacks r)
    row = hub.agents[link["p"]]
    row.bundle_ct = agent_crypto.seal_identity(row.kem, secrets.token_bytes(32), row.id, user.id, other.identity)
    headers = post_form(int(link["c"]), fields)[1]
    assert headers["Location"].endswith("?agent=failed")
    result = run.join()
    assert (result["error"], result["reason"]) == ("pairing_failed", "identity_mismatch")
    assert not (tool.home / "state.json").exists()


def test_me_must_match_the_identity(tool: Tool, hub: HubFake) -> None:
    user, other = hub.add_user(), hub.add_user()
    run, link = start_pair(tool, hub)
    fields = browser_confirms(hub, user, link)
    hub.hooks[("GET", "/v1/me")] = lambda body, request: (
        200,
        {"user_id": user.id, "identity": other.identity.public()},
    )
    assert post_form(int(link["c"]), fields)[1]["Location"].endswith("?agent=failed")
    assert run.join()["error"] == "pairing_failed"


def test_claim_answer_for_another_agent_fails(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    run, link = start_pair(tool, hub)
    fields = browser_confirms(hub, user, link)
    original = hub.r_claim

    def claim(request: object, body: object) -> tuple[int, object]:
        status, answer = original(request, body)  # type: ignore[arg-type]
        return status, {**answer, "agent_id": "01a1ee56-5e00-7242-9e24-f87629ad2977"}

    hub.override("r_claim", claim)
    assert post_form(int(link["c"]), fields)[1]["Location"].endswith("?agent=failed")
    assert run.join()["error"] == "pairing_failed"


def test_me_refused_after_the_claim(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    run, link = start_pair(tool, hub)
    fields = browser_confirms(hub, user, link)
    hub.hooks[("GET", "/v1/me")] = lambda body, request: (503, {"error": "unavailable"})
    assert post_form(int(link["c"]), fields)[1]["Location"].endswith("?agent=failed")
    result = run.join()
    assert result["error"] == "hub_refused"


def test_platform_root_mismatch_fails(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    run, link = start_pair(tool, hub)
    fields = {**browser_confirms(hub, user, link), "k": b64u_encode(secrets.token_bytes(32))}
    assert post_form(int(link["c"]), fields)[1]["Location"].endswith("?agent=failed")
    assert run.join()["reason"] == "platform_root_mismatch"


def test_claim_refused_otherwise_ends_the_pairing(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    run, link = start_pair(tool, hub)
    fields = browser_confirms(hub, user, link)
    hub.hooks[("POST", "/v1/agent/claim")] = lambda body, request: (404, {"error": "pairing_unknown"})
    assert post_form(int(link["c"]), fields)[1]["Location"].endswith("?agent=failed")
    assert run.join()["hub_error"] == "pairing_unknown"


def test_timeout(tool: Tool, hub: HubFake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairing, "PAIRING_TIMEOUT_S", 0.3)
    monkeypatch.setattr(pairing, "POLL_S", 0.05)
    code, document = tool.run("pair", "--hub", hub.origin)
    assert (code, document["error"]) == (1, "pairing_timeout")
    assert "ssh -L" in document["next"]


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--hub", "ftp://x"], "invalid_argument"),
        (["--hub", "http://127.0.0.1:1", "--name", " padded"], "invalid_argument"),
        (["--hub", "http://127.0.0.1:1"], "hub_unreachable"),
    ],
)
def test_pair_argument_and_network_errors(tool: Tool, argv: list[str], error: str) -> None:
    assert tool.run("pair", *argv)[1]["error"] == error


@pytest.mark.parametrize("config", [{"hub_key": {"id": "x", "public_key": "!"}}, {}])
def test_config_without_hub_key(tool: Tool, hub: HubFake, config: dict[str, object]) -> None:
    hub.hooks[("GET", "/v1/config")] = lambda body, request: (200, config)
    assert tool.run("pair", "--hub", hub.origin)[1]["reason"] == "response_invalid"


def test_pairing_answer_must_name_the_pairing(tool: Tool, hub: HubFake) -> None:
    hub.hooks[("POST", "/v1/agent-pairings")] = lambda body, request: (200, {"pairing_id": "other"})
    assert tool.run("pair", "--hub", hub.origin)[1]["reason"] == "response_invalid"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ((429, {"error": "rate_limited"}), {"error": "hub_refused", "hub_error": "rate_limited", "status": 429}),
        ((500, {"error": "Traceback: secret"}), {"error": "hub_refused", "reason": "response_invalid"}),
        ((200, ["not", "an", "object"]), {"error": "hub_refused", "reason": "response_invalid"}),
    ],
)
def test_anonymous_refusals(tool: Tool, hub: HubFake, answer: tuple[int, object], expected: dict[str, object]) -> None:
    hub.hooks[("POST", "/v1/agent-pairings")] = lambda body, request: answer
    document = tool.run("pair", "--hub", hub.origin)[1]
    assert {key: document.get(key) for key in expected} == expected
    assert "secret" not in json.dumps(document)


def test_open_browser_failure_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pairing.open_browser({"AGENT_PLATFORM_BROWSER": str(tmp_path / "missing")}, "http://127.0.0.1:1/connect#x")
    assert "could not be started" in capsys.readouterr().err


def test_default_browser() -> None:
    assert pairing.default_browser("darwin") == "open"
    assert pairing.default_browser("linux") == "xdg-open"


def test_parse_form() -> None:
    pairing_id = "01a1ee56-5e00-7242-9e24-f87629ad2977"
    r, k = b64u_encode(b"r" * 32), b64u_encode(b"k" * 32)
    assert pairing.parse_form(f"p={pairing_id}&r={r}&k={k}".encode(), pairing_id) == pairing.Handover(
        b"r" * 32, k, None
    )
    assert pairing.parse_form(b"\xff", pairing_id) is None
    assert pairing.parse_form(b"p", pairing_id) is None


def test_listener_binds_loopback_only(tmp_path: Path) -> None:
    home = Home(tmp_path / "h", "/nonexistent", False)
    pair = pairing.Pairing(home, "http://127.0.0.1:1", "n", "h")
    ports: list[int] = []

    def ready(port: int) -> None:
        ports.append(port)

    with pytest.raises(PlatformError) as stopped:
        pairing.listen(pair, ready, timeout=0.0)
    assert stopped.value.code == "pairing_timeout"
    assert ports
    assert ports[0] > 0
