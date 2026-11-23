"""U-HUB-invites (expiry job and signal): the package of an expired org invitation is deleted by the job
`invites.expire_packages` (02-data-model.md section 2.1, ADR-005), and every accepted org invitation is announced
once as `invite.accepted` (`org_id`, `invite_id`, `subject_id`) for HUB-15, here received by a fake subscriber."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub import signals
from agent_hub.invites import jobs
from agent_hub.invites import signals as invite_signals
from agent_hub.registry import JOBS, SIGNAL_SUBSCRIBERS, discover
from tests.platform.hub.device.support import Fleet, new_device
from tests.platform.hub.invites.support import HOUR, accept, create_invitation, revoke_invitation
from tests.platform.hub.orgs.support import World

AcceptFlow = Any


@pytest.fixture
def received() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """A fake `invite.accepted` subscriber next to the invites subscriber of `log.appended`."""
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake(name: str, ids: Mapping[str, Any]) -> None:
        seen.append((name, dict(ids)))

    signals.configure({"log.appended": [invite_signals.on_log_appended], "invite.accepted": [fake]})
    yield seen
    signals.configure()


def bind(world: World) -> None:
    jobs.expire_packages(world.hub.app.state.hub)


def packages(world: World) -> dict[str, bool]:
    return {invite: package is not None for invite, package in world.rows("SELECT id, package_ct FROM hub.invite")}


# --- registration -----------------------------------------------------------------------------------------------------


def test_job_and_subscriber_are_registered() -> None:
    assert dict(discover(JOBS))["invites"] == jobs.JOBS
    (job,) = jobs.JOBS
    assert (job.name, job.interval_seconds, job.initial_delay_seconds) == ("invites.expire_packages", 60.0, 0.0)
    assert list(dict(discover(SIGNAL_SUBSCRIBERS))["invites"]["log.appended"]) == [invite_signals.on_log_appended]


# --- expiry -----------------------------------------------------------------------------------------------------------


def test_expired_packages_are_deleted_once(world: World, browser_accept: AcceptFlow) -> None:
    draft = world.public_org()
    short = create_invitation(draft, seconds=HOUR)
    device = create_invitation(draft, kind="device", seconds=HOUR)
    longer = create_invitation(draft, seconds=2 * HOUR)
    revoked = create_invitation(draft, seconds=HOUR)
    assert revoke_invitation(draft, revoked).status_code == 200
    used, _user = browser_accept(draft)
    context = world.hub.app.state.hub
    assert jobs.expire_packages(context) == 0
    world.hub.clock.advance(HOUR)
    assert jobs.expire_packages(context) == 2
    assert packages(world) == {
        short.invite_id: False,
        device.invite_id: False,
        longer.invite_id: True,
        revoked.invite_id: False,
        used.invite_id: False,
    }
    rows = world.rows(
        "SELECT actor_kind, org_id, target_id, detail FROM hub.audit WHERE action = 'invite.expire' ORDER BY seq"
    )
    assert sorted(rows, key=lambda row: row[2]) == sorted(
        [
            ("hub", UUID(draft.id), short.invite_id, {"invite_id": short.invite_id, "kind": "user"}),
            ("hub", UUID(draft.id), device.invite_id, {"invite_id": device.invite_id, "kind": "device"}),
        ],
        key=lambda row: row[2],
    )
    assert jobs.expire_packages(context) == 0
    assert invite_signals._context["hub"] is context


def test_platform_invitations_are_left_alone(world: World) -> None:
    platform_invite = world.platform_invite(world.user())
    world.hub.clock.advance(73 * HOUR)
    assert jobs.expire_packages(world.hub.app.state.hub) == 0
    assert world.rows("SELECT id FROM hub.invite") == [(platform_invite,)]


# --- invite.accepted --------------------------------------------------------------------------------------------------


def test_user_accept_is_announced(
    world: World, browser_accept: AcceptFlow, received: list[tuple[str, dict[str, Any]]]
) -> None:
    bind(world)
    draft = world.public_org()
    invitation, user = browser_accept(draft)
    invite_signals.wait()
    assert received == [
        ("invite.accepted", {"org_id": UUID(draft.id), "invite_id": invitation.invite_id, "subject_id": UUID(user.id)})
    ]


def test_device_enrollment_is_announced(world: World, fleet: Fleet, received: list[tuple[str, dict[str, Any]]]) -> None:
    bind(world)
    draft = world.public_org()
    invitation = create_invitation(draft, kind="device", seconds=24 * HOUR)
    device = new_device()
    assert fleet.enroll_org(draft, device, invitation.secrets).status_code == 200
    invite_signals.wait()
    assert received == [
        (
            "invite.accepted",
            {"org_id": UUID(draft.id), "invite_id": invitation.invite_id, "subject_id": UUID(device.id)},
        )
    ]


def test_other_appends_announce_nothing(world: World, received: list[tuple[str, dict[str, Any]]]) -> None:
    bind(world)
    draft = world.public_org()
    invitation = create_invitation(draft)
    world.hub.clock.advance(1)
    assert revoke_invitation(draft, invitation).status_code == 200
    invite_signals.wait()
    assert received == []


def test_each_accept_of_a_batch_is_announced_once(world: World, received: list[tuple[str, dict[str, Any]]]) -> None:
    # a batch is the run of entries with the receive time of the head; an earlier batch is not announced again
    bind(world)
    draft = world.public_org()
    first, second = world.user(), world.user()
    one = create_invitation(draft)
    two = create_invitation(draft)
    world.hub.clock.advance(1)
    assert accept(draft, first, one).status_code == 200
    world.hub.clock.advance(1)
    assert accept(draft, second, two).status_code == 200
    invite_signals.wait()
    assert [ids["invite_id"] for _name, ids in received] == [one.invite_id, two.invite_id]


def test_nothing_is_announced_without_a_bound_context(
    world: World, browser_accept: AcceptFlow, received: list[tuple[str, dict[str, Any]]]
) -> None:
    draft = world.public_org()
    browser_accept(draft)
    invite_signals.bind(SimpleNamespace(pool=SimpleNamespace(closed=True)))
    invite_signals.on_log_appended("log.appended", {"org_id": UUID(draft.id), "seq": 2})
    invite_signals.wait()
    assert received == []


def test_failures_are_logged(caplog: pytest.LogCaptureFixture, received: list[tuple[str, dict[str, Any]]]) -> None:
    def broken() -> None:
        raise RuntimeError("no connection")

    invite_signals.bind(SimpleNamespace(pool=SimpleNamespace(closed=False, connection=broken)))
    invite_signals.on_log_appended("log.appended", {"org_id": UUID(int=1), "seq": 0})
    invite_signals.wait()
    assert received == []
    assert "invite.accepted could not be announced" in caplog.text
