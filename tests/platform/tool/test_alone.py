"""The refusals of `register --own-account`, `recover` and `agents confirm` (rule E-27, KOR-62; `agent_platform.alone`)
that the real-hub suite (`realhub/test_alone.py`) cannot provoke: malformed inputs, answers of a hub that does not
fit the request, and failures that are passed on unchanged. The hub is replaced by a scripted `anon_request`."""

from __future__ import annotations

import io
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_platform import alone, cli
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.bundle import RECOVERY_SLOT, seal_bundle
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.kdf import kek_from_recovery_code
from agent_runner.crypto.recovery import format_recovery_code, generate_recovery_code

ORIGIN = "http://localhost:8787"
HUB_KEY = {"id": "hub-1", "public_key": b64u_encode(bytes(32))}
ROOT = b64u_encode(bytes(range(32)))
Answer = Callable[[str, str, Any], dict[str, Any]]


def invite_link() -> str:
    return f"{ORIGIN}/invite#{invite_format.format_code(secrets.token_bytes(16))}"


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return Home.from_env({"AGENT_PLATFORM_HOME": str(tmp_path / "home"), "AGENT_PLATFORM_SECURITY": "/nonexistent"})


def script(monkeypatch: pytest.MonkeyPatch, answer: Answer, root: object = ROOT) -> list[tuple[str, str]]:
    """Replace the hub: `answer(method, path, body)` gives each anonymous answer; the calls made."""
    calls: list[tuple[str, str]] = []

    def request(origin: str, method: str, path: str, body: object | None = None) -> dict[str, Any]:
        assert origin == ORIGIN
        calls.append((method, path))
        return answer(method, path, body)

    monkeypatch.setattr(alone, "anon_request", request)
    monkeypatch.setattr(alone, "hub_config", lambda origin: (HUB_KEY, root))
    return calls


def register(home: Home, **changes: Any) -> dict[str, Any]:
    options: dict[str, Any] = {"invite": invite_link(), "display_name": "Bot", "name": "Bot", "host": "box"}
    return alone.register_own(home, **{**options, **changes})


def refusal(work: Callable[[], object]) -> PlatformError:
    with pytest.raises(PlatformError) as refused:
        work()
    return refused.value


def test_register_refuses_bad_names_and_a_bad_root_key_before_anything_is_created(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = script(monkeypatch, lambda method, path, body: {"scope": "platform"})
    assert refusal(lambda: register(home, display_name=" 　 ")).code == "invalid_argument"
    assert refusal(lambda: register(home, name="n" * 65)).code == "invalid_argument"
    assert calls == []
    assert refusal(lambda: register(home, root_key="short")).code == "invalid_argument"
    assert ("POST", "/v1/agent-accounts") not in calls


def test_register_checks_the_invitation_and_the_hubs_answers(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    script(monkeypatch, lambda method, path, body: {"registered": True})
    taken = refusal(lambda: register(home))
    assert (taken.code, taken.fields["hub_error"]) == ("hub_refused", "invite_registered")
    script(monkeypatch, lambda method, path, body: {"scope": "device"})
    assert refusal(lambda: register(home)).fields["reason"] == "response_invalid"
    script(monkeypatch, lambda method, path, body: {"scope": "platform"}, root=None)
    assert refusal(lambda: register(home)).fields["reason"] == "root_key"
    answers = {"/v1/invites/lookup": {"scope": "org"}, "/v1/agent-accounts": {"user_id": uuid7(), "agent_id": "x"}}
    script(monkeypatch, lambda method, path, body: answers[path])
    assert refusal(lambda: register(home)).fields["reason"] == "response_invalid"
    assert not (home.path / "state.json").exists()


@dataclass
class Begun:
    """A recovery code and the answer of `recovery/begin` for it."""

    code: bytes = field(default_factory=generate_recovery_code)
    user: str = field(default_factory=uuid7)

    def answer(self) -> dict[str, Any]:
        identity = alone.new_identity()
        sealed = seal_bundle(kek_from_recovery_code(self.code), self.user, RECOVERY_SLOT, identity)
        return {"user_id": self.user, "bundle_ct": sealed, "challenge": b64u_encode(bytes(32))}


def recover(home: Home, code: bytes, **changes: Any) -> dict[str, Any]:
    options = {"hub": ORIGIN, "code_text": format_recovery_code(code), "name": "Bot", "host": "box"}
    return alone.recover(home, **{**options, **changes})


def test_recover_refuses_bad_inputs_and_passes_other_refusals_on(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    begun = Begun()
    calls = script(monkeypatch, lambda method, path, body: begun.answer())
    assert refusal(lambda: recover(home, begun.code, hub="ftp://x")).code == "invalid_argument"
    assert refusal(lambda: recover(home, begun.code, code_text="nonsense")).code == "invalid_argument"
    assert refusal(lambda: recover(home, begun.code, host="")).code == "invalid_argument"
    assert calls == []

    def limited(method: str, path: str, body: Any) -> dict[str, Any]:
        raise PlatformError("hub_refused", hub_error="rate_limited", status=429)

    script(monkeypatch, limited)
    assert refusal(lambda: recover(home, begun.code)).fields["hub_error"] == "rate_limited"
    script(monkeypatch, lambda method, path, body: {**begun.answer(), "user_id": "nobody"})
    assert refusal(lambda: recover(home, begun.code)).fields["reason"] == "recovery_bundle_invalid"
    script(monkeypatch, lambda method, path, body: begun.answer())
    assert refusal(lambda: recover(home, Begun(user=begun.user).code)).fields["reason"] == "recovery_bundle_invalid"


def test_recover_checks_the_finish(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    begun = Begun()

    def finish_refused(method: str, path: str, body: Any) -> dict[str, Any]:
        if path == "/v1/recovery/begin":
            return begun.answer()
        raise PlatformError("hub_refused", hub_error="signature_invalid", status=401)

    script(monkeypatch, finish_refused)
    assert refusal(lambda: recover(home, begun.code)).fields["hub_error"] == "signature_invalid"
    answers = {"/v1/recovery/begin": begun.answer(), "/v1/recovery/finish": {"user_id": begun.user, "agent_id": "x"}}
    script(monkeypatch, lambda method, path, body: answers[path])
    assert refusal(lambda: recover(home, begun.code)).fields["reason"] == "response_invalid"


def test_the_recover_command_needs_a_code(tmp_path: Path) -> None:
    out = io.StringIO()
    environ = {"AGENT_PLATFORM_HOME": str(tmp_path / "home")}
    assert cli.run(["recover", "--hub", ORIGIN], environ, io.StringIO(" \n"), out) == 1
    assert '"error": "invalid_argument"' in out.getvalue()


def test_storing_a_new_agent_replaces_the_previous_state(home: Home) -> None:
    identity = alone.new_identity()
    first = alone.Sealed(uuid7(), identity, "Bot", "box")
    first.store(home, ORIGIN, HUB_KEY, ROOT)
    second = alone.Sealed(first.user_id, identity, "Bot", "box")
    second.store(home, ORIGIN, HUB_KEY, ROOT)
    assert home.load()["agent_id"] == second.agent_id


# --- agents confirm ---


@dataclass
class FakeAccount:
    """The paired account `confirm` works on."""

    home: Home
    platform_root: str | None = ROOT
    posted: list[dict[str, Any]] = field(default_factory=list)
    answer: Callable[[dict[str, Any]], dict[str, Any]] = lambda body: {"agent_id": body["pairing_id"]}
    origin: str = ORIGIN
    user_id: str = field(default_factory=uuid7)
    agent_id: str = field(default_factory=uuid7)
    identity: Any = field(default_factory=alone.new_identity)
    state: dict[str, Any] = field(default_factory=dict)

    def call(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posted.append(body)
        return self.answer(body)


@dataclass
class Remote:
    """A remote pairing of a new agent at the hub: its link and agent code."""

    pairing_id: str = field(default_factory=uuid7)
    agent: Any = field(default_factory=agent_crypto.new_agent)
    nonce: bytes = field(default_factory=lambda: secrets.token_bytes(16))

    @property
    def fp(self) -> str:
        return agent_crypto.fingerprint(self.agent.kem_public(), self.agent.sig_public())

    def link(self, **changes: str) -> str:
        fields = {"p": self.pairing_id, "f": self.fp, "m": "remote", "a": "Bot", "h": "box", **changes}
        return f"{ORIGIN}/connect#" + "&".join(f"{key}={value}" for key, value in fields.items())

    def code(self) -> str:
        return agent_crypto.format_code(agent_crypto.agent_code(self.pairing_id, self.fp, self.nonce, ORIGIN))

    def lookup(self, **changes: object) -> dict[str, Any]:
        found = {
            "pairing_id": self.pairing_id,
            "pub": self.agent.public(),
            "state": "open",
            "remote": True,
            "nonce": b64u_encode(self.nonce),
        }
        return {**found, **changes}


def confirm(account: FakeAccount, link: str, code: str) -> dict[str, Any]:
    return alone.confirm(account, link=link, code=code)  # type: ignore[arg-type]


def test_confirm_refuses_links_that_are_no_remote_pairing_of_this_hub(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairing = Remote()
    calls = script(monkeypatch, lambda method, path, body: pairing.lookup())
    account = FakeAccount(home)
    for link in (
        pairing.link() + "&p=" + pairing.pairing_id,
        pairing.link(m="loopback"),
        pairing.link(p="not-a-uuid"),
        pairing.link().replace("&h=box", ""),
        pairing.link(a="%E2%80%AE"),
        pairing.link().replace("/connect#", "/connect?x=1#"),
        pairing.link().replace(ORIGIN, "https://elsewhere.example"),
    ):
        with pytest.raises(PlatformError) as refused:
            confirm(account, link, pairing.code())
        assert refused.value.code == "invalid_argument"
    assert refusal(lambda: confirm(account, pairing.link(), "short")).code == "invalid_argument"
    assert calls == []


def test_confirm_needs_the_open_remote_pairing_of_the_link(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    pairing = Remote()
    account = FakeAccount(home)
    changed: list[dict[str, object]] = [{"state": "confirmed"}, {"remote": None}, {"pub": None}, {"nonce": "x"}]
    for changes in changed:
        found = pairing.lookup(**changes)

        def answer(method: str, path: str, body: Any, found: dict[str, Any] = found) -> dict[str, Any]:
            return found

        script(monkeypatch, answer)
        with pytest.raises(PlatformError) as refused:
            confirm(account, pairing.link(), pairing.code())
        assert refused.value.fields["reason"] == "pairing_mismatch"
    assert account.posted == []


def test_confirm_needs_the_root_key_and_checks_the_hubs_answer(home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    pairing = Remote()
    script(monkeypatch, lambda method, path, body: pairing.lookup())
    assert refusal(lambda: confirm(FakeAccount(home, None), pairing.link(), pairing.code())).code == "state_damaged"

    def refused(body: dict[str, Any]) -> dict[str, Any]:
        raise PlatformError("hub_refused", hub_error="pairing_unknown", status=404)

    passed_on = refusal(lambda: confirm(FakeAccount(home, answer=refused), pairing.link(), pairing.code()))
    assert passed_on.fields["hub_error"] == "pairing_unknown"
    other = FakeAccount(home, answer=lambda body: {"agent_id": uuid7()})
    assert refusal(lambda: confirm(other, pairing.link(), pairing.code())).fields["reason"] == "response_invalid"
