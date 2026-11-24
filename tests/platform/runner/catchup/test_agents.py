"""U-RUN-crypto / catch-up wrapping for agents (ADR-082 sections 1 and 9.4): the device wraps what it holds to the
active agent of a user who reads it - scope keys and the user's `CK` - and never to a removed or superseded agent; a
`cred` scope is a user's only."""

from __future__ import annotations

from collections.abc import Callable

from agent_runner.catchup import plan
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.verify import verify_log
from agent_runner.crypto.log.wraps import WrapSlot
from agent_runner.orgs.book import OrgBook

ALICE, BOT = v.pid("alice"), v.pid("bot")
HELD = {("org", v.ORG, 1), ("wg", v.W0, 1), ("cred", ALICE, 1)}


def org(*steps: Callable[[v.LogBuilder], object]) -> v.LogBuilder:
    """alice founds the org, the device serves the default workgroup, alice adds her agent `bot`, then `steps`."""
    log = v.LogBuilder()
    log.genesis()
    log.invite_device("alice", "device", [v.W0])
    log.accept("device", "device")
    log.agent_add("alice", "bot", "alice", 1)
    for step in steps:
        step(log)
    return log


def book(*logs: v.LogBuilder) -> OrgBook:
    device = v.pub("device")
    found = OrgBook(
        device_id=v.pid("device"), kem_public=device["kem"], sig_public=device["sig"], root=v.sig_pk("root"), kind="org"
    )
    verified = [verify_log(log.entries, v.sig_pk("alice")) for log in logs]
    found.logs = {log.state.org: log for log in verified}
    return found


def to_bot(found: OrgBook, held: set[tuple[str, str, int]] = HELD) -> set[WrapSlot]:
    return {slot for slot in plan.plan(found, held, plan.Memory()) if slot.recipient == BOT}


def test_the_device_wraps_every_key_it_holds_of_the_user_to_the_users_active_agent() -> None:
    found = book(org())
    assert to_bot(found) == {
        WrapSlot("org", v.ORG, 1, BOT),
        WrapSlot("wg", v.W0, 1, BOT),
        WrapSlot("cred", ALICE, 1, BOT),
    }
    assert plan.recipient_kem(found, WrapSlot("cred", ALICE, 1, BOT)) == v.pub("bot")["kem"]
    # an older epoch of the user's slot the device uploaded is the agent's slot too
    memory = plan.Memory(done={WrapSlot("cred", ALICE, 1, ALICE)})
    planned = plan.plan(found, {*HELD, ("cred", ALICE, 2)}, memory)
    assert {s for s in planned if s.recipient == BOT and s.scope == "cred"} == {
        WrapSlot("cred", ALICE, 1, BOT),
        WrapSlot("cred", ALICE, 2, BOT),
    }


def test_a_removed_agent_gets_nothing() -> None:
    assert to_bot(book(org(lambda log: log.add("remove", "alice", subject=BOT)))) == set()


def test_a_superseded_agent_gets_nothing_also_where_it_is_still_active() -> None:
    other = v.LogBuilder(v.ORG_B)
    other.genesis(default_wg=v.WB)
    other.invite_device("alice", "device-b", [v.WB])
    other.accept("device", "device", code_label="device-b")
    other.agent_add("alice", "bot6", "alice", 2)
    assert to_bot(book(org(), other)) == set()


def test_a_cred_scope_keyed_by_an_agent_is_never_planned() -> None:
    assert [s for s in plan.plan(book(org()), {("cred", BOT, 1)}, plan.Memory()) if s.scope == "cred"] == []
