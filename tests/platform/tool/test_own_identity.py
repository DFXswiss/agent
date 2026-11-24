"""The agent tool's own identity (ADR-082) in the parts the real-hub suite cannot reach: which logs an agent trusts,
the checks of a conversion's anchor, the clean-up of revoked agents that are still active in a log, a record of a
foreign owner, the refusals and the retry of a delegation, and the upkeep report of the clean-up. The logs are built
with the reference builders (`agent_runner.crypto.log.vectors`) and verified by the copied verifier."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from agent_platform import convert, credentials, credkey, delegate, membership, revoke, selfadd, upkeep
from agent_platform.account import Account
from agent_platform.batch import build_batch
from agent_platform.errors import PlatformError
from agent_platform.hub import AgentClient
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto.credential import new_header, seal_credential
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.verify import VerifiedLog, verify_log

ALICE = v.pid("alice")
Json = dict[str, Any]


def verified(log: v.LogBuilder, founder: str = "alice") -> VerifiedLog:
    return verify_log(log.entries, v.sig_pk(founder))


def own_account(agent: str, logs: dict[str, VerifiedLog] | None = None) -> Account:
    """An own-identity account of alice's agent `agent` (no hub: only what `_trusts` reads)."""
    found = Account(
        home=None,  # type: ignore[arg-type]
        state={"user_id": ALICE, "agent_id": v.pid(agent)},
        agent=v.identity(agent),
        identity=None,
        client=None,  # type: ignore[arg-type]
        user_pub=v.pub("alice"),
    )
    found._logs = dict(logs or {})
    return found


# --- trust ---


def superseding() -> VerifiedLog:
    """A log of alice in which her agent `bot6` (generation 2) supersedes `bot` in every log."""
    other = v.LogBuilder(v.ORG_B)
    other.genesis(default_wg=v.WB)
    other.agent_add("alice", "bot6", "alice", 2)
    return verified(other)


def test_a_log_founded_through_a_revoked_agent_is_trusted_only_where_this_agent_was_added() -> None:
    founded = v.LogBuilder()
    founded.genesis(agent="bot")  # signed by bot, which bot6 supersedes
    account = own_account("bot6", {v.ORG_B: superseding()})
    assert account._trusts(verified(founded)) is False
    founded.agent_add("alice", "bot6", "alice", 2)
    assert account._trusts(verified(founded)) is True
    # without the superseding log the signing agent is not revoked: the genesis proves the user
    assert own_account("bot6")._trusts(verified(v.LogBuilder(entries=founded.entries[:1]))) is True


def test_a_log_whose_user_has_other_keys_is_never_trusted() -> None:
    founded = v.LogBuilder()
    founded.genesis()
    account = own_account("bot")
    account.user_pub = v.pub("bob")
    assert account._trusts(verified(founded)) is False


# --- conversion ---


def test_an_anchor_that_does_not_open_or_names_other_keys_is_refused() -> None:
    agent, secret = v.identity("bot"), bytes(range(32))
    state = {"agent_id": v.pid("bot"), "user_id": ALICE, "secret": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"}
    account = SimpleNamespace(state=state, agent=agent, user_pub=v.pub("alice"))
    with pytest.raises(PlatformError) as refused:
        convert._adopt(account, "not-sealed", 1)  # type: ignore[arg-type]
    assert refused.value.fields["reason"] == "identity_mismatch"
    cert = agent_crypto.sign_cert(v.identity("bob").sig, v.pid("bot"), agent.public(), ALICE, 1)
    foreign = agent_crypto.seal_user(agent.kem_public(), secret, v.pid("bot"), ALICE, user_pub=v.pub("bob"), cert=cert)
    with pytest.raises(PlatformError) as refused:
        convert._adopt(account, foreign, 1)  # type: ignore[arg-type]
    assert refused.value.fields["reason"] == "identity_mismatch"


# --- clean-up of revoked agents ---


@dataclass
class Stub:
    """An account of alice's agent with verified logs and a scripted hub list of agents."""

    logs: dict[str, VerifiedLog]
    agent_id: str
    listed: list[Json] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    user_id: str = ALICE
    mode: str = "own"
    state: dict[str, Any] = field(default_factory=dict)
    home: Any = None

    def me(self) -> Json:
        return {"orgs": [{"org_id": org} for org in self.logs] + [{"no": "id"}]}

    def org_log(self, org: str) -> VerifiedLog:
        return self.logs[org]

    def call(self, method: str, path: str, body: object = None) -> Json:
        self.calls.append((method, path))
        return {"agents": self.listed} if method == "GET" else {"revoked": 2}


def removals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[dict[str, object]] | None]]:
    """`membership.submit` replaced: the planned entries per org (None: nothing to do)."""
    seen: list[tuple[str, list[dict[str, object]] | None]] = []

    def submit(account: Any, org: str, planner: Callable[..., Any]) -> None:
        planned = planner(account.org_log(org), account.user_id)
        seen.append((org, planned))

    monkeypatch.setattr(membership, "submit", submit)
    return seen


def two_agents() -> VerifiedLog:
    """alice's org with her agent `bot` (generation 1) active."""
    log = v.LogBuilder()
    log.genesis()
    log.agent_add("alice", "bot", "alice", 1)
    return verified(log)


def test_cleanup_removes_revoked_agents_the_logs_or_the_hub_name_and_keeps_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = removals(monkeypatch)
    org = v.ORG
    # superseded in another verified log: removed without asking the hub
    stub = Stub({org: two_agents(), v.ORG_B: superseding()}, v.pid("bot6"))
    assert [item["agent_id"] for item in revoke.cleanup(stub)] == [v.pid("bot")]  # type: ignore[arg-type]
    assert seen == [(org, [{"type": "remove", "subject": v.pid("bot")}])]
    assert stub.calls == []
    # active at the hub: kept; revoked at the hub: removed
    seen.clear()
    stub = Stub({org: two_agents()}, v.pid("bot9"), listed=[{"agent_id": v.pid("bot"), "state": "active"}, {}])
    assert revoke.cleanup(stub) == []  # type: ignore[arg-type]
    assert stub.calls == [("GET", "/v1/me/agents")]
    stub.listed = [{"agent_id": v.pid("bot"), "state": "revoked"}]
    assert [item["changed"] for item in revoke.cleanup(stub)] == [False]  # type: ignore[arg-type]
    # an agent that is no longer active where the planner looks plans nothing
    gone = v.LogBuilder(entries=list(two_agents().entries))
    gone.add("remove", "alice", subject=v.pid("bot"))
    assert revoke.remove(Stub({org: verified(gone)}, v.pid("bot9")), org, v.pid("bot"))["changed"] is False  # type: ignore[arg-type]
    assert seen[-1] == (org, None)


def test_revoking_another_agent_removes_it_and_rotates_the_credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    removals(monkeypatch)
    monkeypatch.setattr(revoke, "remove", lambda account, org, agent: {"org_id": org, "changed": True})
    rotations: list[str] = []

    def rotated(account: Any) -> Json:
        rotations.append("x")
        return {"status": "rotated"}

    monkeypatch.setattr(credkey, "rotate_if_owed", rotated)
    stub = Stub({v.ORG: two_agents()}, v.pid("bot9"))
    result = revoke.agents_revoke(stub, v.pid("bot"), False)  # type: ignore[arg-type]
    assert (result["revoked"], result["this_agent_revoked"], result["credentials"]) == (1, False, {"status": "rotated"})
    assert stub.calls == [("DELETE", f"/v1/me/agents/{v.pid('bot')}")]
    # --all: the others first, then this agent leaves and the hub revokes every agent
    deleted: list[tuple[str, Any]] = []
    stub = Stub({v.ORG: two_agents()}, v.pid("bot9"), home=SimpleNamespace(delete=lambda *a: deleted.append(a)))
    every = revoke.agents_revoke(stub, None, True)  # type: ignore[arg-type]
    assert (every["revoked"], every["this_agent_revoked"], every["left"]) == (2, True, [])
    assert stub.calls == [("DELETE", "/v1/me/agents")]
    assert rotations == ["x", "x"]


def test_a_failing_cleanup_is_reported_and_a_removal_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(account: Any) -> list[Json]:
        raise PlatformError("hub_refused", hub_error="unavailable")

    monkeypatch.setattr(revoke, "cleanup", refused)
    monkeypatch.setattr(selfadd, "add_everywhere", lambda account: ([], []))
    monkeypatch.setattr(upkeep, "_orgs", lambda account, report: [])
    monkeypatch.setattr(credkey, "rotate_if_owed", lambda account: {"status": "not_owed"})
    report: Json = {}
    upkeep.prepare(None, report)  # type: ignore[arg-type]
    assert report["errors"][0]["step"] == "agents"
    monkeypatch.setattr(revoke, "cleanup", lambda account: [{"agent_id": "a"}])
    report = {}
    upkeep.prepare(None, report)  # type: ignore[arg-type]
    assert report == {"removed_agents": [{"agent_id": "a"}]}


# --- a record of another owner, a delegation that fails or races ---


def test_a_record_of_another_owner_has_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    key = bytes(32)
    fields = new_header(id=uuid7(), owner=v.pid("bob"), kind="github", version=1, epoch=1, author=ALICE, ts=v.ts(0))
    record = seal_credential(key, fields, body_json({"token": "x"}), v.identity("alice").sig)
    account = SimpleNamespace(
        call=lambda method, path: {"record": record},
        author_key=lambda author: v.identity("alice").sig_public(),
        cred_keys=lambda: {1: key},
        user_id=ALICE,
    )
    assert credentials.read_record(account, "github") == (record, None)  # type: ignore[arg-type]


def test_a_batch_the_builder_refuses_and_a_moved_log_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    log = v.LogBuilder()
    log.genesis()
    account = SimpleNamespace(
        identity=v.identity("alice"),
        user_id=ALICE,
        org_log=lambda org: verified(log),
        keys=lambda org: {},
        me=lambda: {"orgs": [{"org_id": v.ORG}]},
    )
    add = {"type": "agent_add", "subject": v.pid("bot"), "user": ALICE, "pub": v.pub("bot"), "gen": 0, "cert": "x"}
    with pytest.raises(PlatformError) as refused:
        delegate._batch(account, v.ORG, add)  # type: ignore[arg-type]
    assert refused.value.code == "hub_refused"
    forgotten: list[str] = []
    account.forget = forgotten.append
    tries: list[int] = []

    def send() -> Json:
        tries.append(1)
        if len(tries) < 3:
            raise PlatformError("hub_refused", hub_error="seq_conflict")
        return {"ok": True}

    assert delegate.submit(account, send) == {"ok": True}  # type: ignore[arg-type]
    assert (len(tries), forgotten) == (3, [v.ORG, v.ORG])
    tries.clear()

    def always() -> Json:
        tries.append(1)
        raise PlatformError("hub_refused", hub_error="seq_conflict")

    with pytest.raises(PlatformError):
        delegate.submit(account, always)  # type: ignore[arg-type]
    assert len(tries) == 3


# --- the epochs a new agent gets in a batch ---


def test_a_newly_entitled_agent_gets_exactly_its_users_epochs() -> None:
    log = v.LogBuilder()
    log.genesis()
    log.add("wg_create", "alice", wg=v.W1, epoch=1)
    log.add("rotate", "alice", scope="wg", scope_id=v.W1, epoch=2)
    log.invite_user("alice", "bob", wgs={})
    log.accept("bob")
    log.agent_add("bob", "bot2", "bob", 1)
    held = {("wg", v.W1, epoch): bytes([epoch]) * 32 for epoch in (1, 2)}

    def wrapped(history: bool, agent_history: bool = False) -> set[tuple[str, int]]:
        built = build_batch(
            verified(log),
            org=v.ORG,
            author=ALICE,
            identity=v.identity("alice"),
            fields=[{"type": "grant", "subject": v.pid("bob"), "wg": v.W1, "access": "read"}],
            ts=v.ts(30),
            held=held,
            history=history,
            agent_history=agent_history,
        )
        return {(str(w["recipient_id"]), int(str(w["epoch"]))) for w in built.wraps}

    bob, bot = v.pid("bob"), v.pid("bot2")
    assert wrapped(False) == {(bob, 2), (bot, 2)}
    assert wrapped(True) == {(bob, 1), (bob, 2), (bot, 1), (bot, 2)}
    assert wrapped(False, agent_history=True) == {(bob, 2), (bot, 1), (bot, 2)}


# --- self-add, a closed client ---


def test_the_self_add_reports_refusals_and_skips_without_an_org_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(account: Any, org: str) -> Json:
        raise PlatformError("hub_refused", hub_error="log_agent")

    monkeypatch.setattr(selfadd, "add", refused)
    stub = Stub({v.ORG: two_agents()}, v.pid("bot9"))
    added, failed = selfadd.add_everywhere(stub)  # type: ignore[arg-type]
    assert (added, [org for org, _error in failed]) == ([], [v.ORG, "None"])

    def unlisted() -> Json:
        raise PlatformError("hub_unreachable")

    stub.me = unlisted  # type: ignore[method-assign]
    assert selfadd.add_everywhere(stub) == ([], [])  # type: ignore[arg-type]
    monkeypatch.setattr(revoke, "cleanup", lambda account: [])
    monkeypatch.setattr(selfadd, "add_everywhere", lambda account: ([], [(v.ORG, PlatformError("hub_unreachable"))]))
    monkeypatch.setattr(upkeep, "_orgs", lambda account, report: [])
    monkeypatch.setattr(credkey, "rotate_if_owed", lambda account: {"status": "not_owed"})
    report: Json = {}
    upkeep.prepare(None, report)  # type: ignore[arg-type]
    assert [(item["step"], item["org_id"]) for item in report["errors"]] == [("agent_add", v.ORG)]


def test_closing_a_client_twice_does_nothing() -> None:
    client = AgentClient("http://127.0.0.1:9", v.pid("bot"), v.identity("bot"), bytes(32))
    client.close()
    client.close()


def test_a_self_add_the_verifier_refuses_is_a_hub_refusal() -> None:
    log = v.LogBuilder()
    log.genesis()
    account = SimpleNamespace(
        agent_id=v.pid("bot"),
        user_id=ALICE,
        agent=v.identity("bot"),
        state={"gen": 1},
        cert=v.certificate("bot", "bob", 1),  # not signed by the user
        org_log=lambda org: verified(log),
        keys=lambda org: {},
    )
    with pytest.raises(PlatformError) as refused:
        selfadd.add(account, v.ORG)  # type: ignore[arg-type]
    assert refused.value.code == "hub_refused"


# --- `CK` wraps of a former agent ---


def cred_wrap(wrapper: str, epoch: int, recipient: str = "bot6") -> Json:
    return {**v.make_wrap(wrapper, "cred", ALICE, epoch, recipient), "wrapped_by": v.pid(wrapper)}


def test_a_ck_wrap_of_a_former_agent_opens_but_is_never_sealed_under() -> None:
    account = own_account("bot6")
    account._former = {v.pid("bot"): v.identity("bot").sig_public()}
    account._wraps = [
        cred_wrap("alice", 1),
        cred_wrap("bot", 2),  # the former agent rotated before it was revoked
        cred_wrap("bob", 3),  # a stranger
        cred_wrap("bot", 4, recipient="bot7"),  # to another agent
    ]
    assert account.cred_keys() == {1: v.scope_key("cred", ALICE, 1), 2: v.scope_key("cred", ALICE, 2)}
    assert account.cred_key() == (1, v.scope_key("cred", ALICE, 1))
    with pytest.raises(CryptoError):
        account.author_key(v.pid("bob"))
    # the newest epoch only a former agent wrapped makes the rotation owed
    latest = credkey.own_keys(account)[0]
    assert (latest.epoch, latest.sealable) == (2, False)
    assert credkey._owed(account, latest, [], datetime.now(UTC)) is True
    # a later wrap of the same epoch by the user makes it sealable
    account._wraps.append(cred_wrap("alice", 2))
    assert account.cred_epochs()[2][1] is True


def test_former_agents_are_read_once_from_the_verified_logs() -> None:
    founded = v.LogBuilder()
    founded.genesis()
    founded.agent_add("alice", "bot", "alice", 1)
    founded.agent_add("alice", "bot6", "alice", 2)
    account = own_account("bot6", {v.ORG: verified(founded)})
    listed: list[int] = []

    def me() -> Json:
        listed.append(1)
        return {"orgs": [{"org_id": v.ORG}, {}]}

    account.me = me  # type: ignore[method-assign]
    assert account.former_agents() == {v.pid("bot"): v.identity("bot").sig_public()}
    assert account.former_agents() == {v.pid("bot"): v.identity("bot").sig_public()}
    assert listed == [1]
    assert account.author_key(v.pid("bot")) == v.identity("bot").sig_public()

    def unreachable() -> Json:
        raise PlatformError("hub_unreachable")

    account._former = None
    account.me = unreachable  # type: ignore[method-assign]
    assert account.former_agents() == {}
