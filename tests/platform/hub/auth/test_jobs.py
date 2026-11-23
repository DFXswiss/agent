"""U-HUB-passkeys (jobs): orphaned registrations are released after 24 h (03-api.md section 4.1, ADR-026) and
ceremonies are purged after one day (02-data-model.md section 2.1); fake clock throughout."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any

import pytest

from agent_core.signing import b64url_encode
from agent_hub.auth import jobs
from agent_hub.auth.ceremonies import credential_reference
from agent_hub.auth.fields import decode
from agent_hub.errors import HubError
from agent_hub.jobs import Job
from agent_hub.registry import JOBS, concat_sequences
from tests.platform.hub.auth.authenticator import Account, FakeBrowser, seed_invite
from tests.platform.hub.conftest import HubHarness

DAY = 24 * 3600
USER_TABLES = (
    ("principal", "id"),
    ("user_identity", "user_id"),
    ("user_settings", "user_id"),
    ("identity_bundle", "user_id"),
    ("passkey", "user_id"),
    ("browser_session", "user_id"),
    ("ceremony", "user_id"),
    ("key_wrap", "recipient_id"),
    ("scope_epoch", "scope_id"),
)


def rows(hub: HubHarness, query: str, *params: Any) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return list(connection.execute(query, params).fetchall())


def user_rows(hub: HubHarness, user_id: str) -> dict[str, int]:
    return {
        table: int(rows(hub, f"SELECT count(*) FROM hub.{table} WHERE {column} = %s", uuid.UUID(user_id))[0][0])
        for table, column in USER_TABLES
    }


def release(hub: HubHarness) -> int:
    return jobs.release_orphan_registrations(hub.app.state.hub)


@pytest.fixture
def orphan(browser: FakeBrowser) -> tuple[str, Account]:
    invite = seed_invite(browser.hub)
    account = browser.register(invite)
    browser.login(account)
    return invite, account


def test_the_jobs_are_registered() -> None:
    names = [job.name for job in concat_sequences(JOBS)]
    assert {"auth.orphan_registrations", "auth.ceremony_purge"} <= set(names)
    assert all(isinstance(job, Job) for job in jobs.JOBS)


def test_a_registration_without_accept_is_released_after_24_hours(
    browser: FakeBrowser, orphan: tuple[str, Account]
) -> None:
    hub = browser.hub
    invite, account = orphan
    assert all(user_rows(hub, account.user_id).values())
    hub.clock.advance(DAY - 1)
    assert release(hub) == 0
    hub.clock.advance(1)
    assert release(hub) == 1
    assert set(user_rows(hub, account.user_id).values()) == {0}
    assert rows(hub, "SELECT registered_user, registered_at FROM hub.invite") == [(None, None)]
    [(actor_kind, target, detail)] = rows(
        hub, "SELECT actor_kind, target_id, detail FROM hub.audit WHERE action = 'invite.release'"
    )
    assert (actor_kind, target, detail) == ("hub", invite, {"invite_id": invite, "kind": "user"})
    # The invitation is usable again until it expires.
    assert browser.register_begin(invite).status_code == 200
    assert release(hub) == 0


def test_an_accepted_invitation_keeps_the_user(browser: FakeBrowser, orphan: tuple[str, Account]) -> None:
    hub = browser.hub
    _, account = orphan
    with hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.invite SET used_at = %s, used_by = %s", (hub.clock(), uuid.UUID(account.user_id))
        )
    hub.clock.advance(2 * DAY)
    assert release(hub) == 0
    assert user_rows(hub, account.user_id)["principal"] == 1


@pytest.mark.parametrize(
    "membership",
    [
        "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq) VALUES (%(org)s, %(user)s, 'user',"
        " 'member', 1)",
        "INSERT INTO hub.platform_role (user_id, role, granted_seq) VALUES (%(user)s, 'inviter', 1)",
    ],
    ids=["org-member", "platform-role"],
)
def test_a_user_who_joined_otherwise_is_kept(
    browser: FakeBrowser, orphan: tuple[str, Account], membership: str
) -> None:
    hub = browser.hub
    _, account = orphan
    with hub.pool.connection() as connection:
        connection.execute(membership, {"org": uuid.uuid4(), "user": uuid.UUID(account.user_id)})
    hub.clock.advance(2 * DAY)
    assert release(hub) == 0
    assert user_rows(hub, account.user_id)["passkey"] == 1


def test_only_the_orphan_is_released(browser: FakeBrowser, orphan: tuple[str, Account]) -> None:
    hub = browser.hub
    _, first = orphan
    hub.clock.advance(DAY)
    second = browser.register(seed_invite(hub))
    assert release(hub) == 1
    assert user_rows(hub, first.user_id)["principal"] == 0
    assert user_rows(hub, second.user_id)["principal"] == 1


def test_ceremonies_are_purged_after_one_day(browser: FakeBrowser) -> None:
    hub = browser.hub
    browser.login_begin()
    hub.clock.advance(DAY - 1)
    browser.login_begin()
    assert jobs.purge_old_ceremonies(hub.app.state.hub) == 0
    hub.clock.advance(2)
    assert jobs.purge_old_ceremonies(hub.app.state.hub) == 1
    assert rows(hub, "SELECT count(*) FROM hub.ceremony") == [(1,)]


def test_the_jobs_run_in_the_job_runner(browser: FakeBrowser, orphan: tuple[str, Account]) -> None:
    hub = browser.hub
    hub.clock.advance(DAY + 1)
    context = hub.app.state.hub
    for job in jobs.JOBS:
        job.run(context)
    assert rows(hub, "SELECT count(*) FROM hub.principal") == [(0,)]


def test_credential_reference_and_decode() -> None:
    short = secrets.token_bytes(96)
    assert credential_reference(short) == b64url_encode(short)
    assert credential_reference(secrets.token_bytes(97)).startswith("sha256:")
    assert decode("AQ") == b"\x01"
    with pytest.raises(HubError, match="request_invalid"):
        decode("AB")
    assert timedelta(seconds=jobs.ORPHAN_SECONDS) == timedelta(days=1)
