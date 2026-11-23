"""The hub's own view of stored logs (`agent_hub.orgs.logs`), the events interface, the rebuilders and the append
service's extension step."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.orgs import logs
from agent_hub.orgs.append import AppendResult, append_batch, hub_error
from agent_hub.orgs.projections import REBUILDERS, rebuild_org
from agent_hub.registry import PROJECTION_REBUILDERS, merge_mappings
from tests.platform.hub.orgs.support import PlatformLog, World, insert_platform_entries, new_id


def test_log_cache_evicts_the_least_recently_used() -> None:
    cache = logs.LogCache(size=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)
    assert (cache.get("a"), cache.get("b"), cache.get("c")) == (1, None, 3)
    cache.clear()
    assert cache.get("a") is None


def test_stored_logs_that_do_not_verify_are_never_used(world: World) -> None:
    draft = world.public_org()
    founder = draft.founder
    logs.ORG_LOGS.clear()
    with world.hub.pool.connection() as connection:
        org = logs.org_row(connection, uuid.UUID(draft.id))
        assert org is not None
        with pytest.raises(logs.StoredLogInvalid, match="head"):
            logs.verified_log(connection, logs.replace_head(org, 0, "x" * 43), None)
        assert logs.verified_log(connection, org, None).head.seq == 0
        assert logs.verified_log(connection, org, None) is logs.verified_log(connection, org, None)
        logs.ORG_LOGS.clear()
        connection.execute("DELETE FROM hub.user_identity WHERE user_id = %s", (founder.id,))
        with pytest.raises(logs.StoredLogInvalid, match="identity"):
            logs.verified_log(connection, org, None)
        connection.rollback()
        logs.ORG_LOGS.clear()
        connection.execute("UPDATE hub.user_identity SET sig_pub = %s WHERE user_id = %s", (b"\x01" * 32, founder.id))
        with pytest.raises(logs.StoredLogInvalid, match="membership"):
            logs.verified_log(connection, org, None)
        connection.rollback()
    response = founder.browser.get(f"/v1/orgs/{draft.id}/catch-up")
    assert response.status_code == 200


def test_a_stored_platform_log_that_does_not_verify_is_refused(world: World) -> None:
    platform = PlatformLog()
    platform.add("root", pub="A" * 43)
    insert_platform_entries(world.hub.pool, platform.entries, world.hub.clock())
    with world.hub.pool.connection() as connection, pytest.raises(logs.StoredLogInvalid):
        logs.platform_state(connection, world.hub.config.platform_root_key)
    draft = world.org()
    response = draft.create()
    assert (response.status_code, response.json()) == (503, {"error": "unavailable"})


def test_platform_state_is_cached_by_head(world: World) -> None:
    device = world.device("platform")
    platform = PlatformLog()
    platform.certify(device)
    insert_platform_entries(world.hub.pool, platform.entries, world.hub.clock())
    with world.hub.pool.connection() as connection:
        first = logs.platform_state(connection, world.hub.config.platform_root_key)
        assert first is not None
        assert logs.platform_state(connection, world.hub.config.platform_root_key) is first


def test_hub_error_keeps_codes_and_maps_reader_codes() -> None:
    assert hub_error(CryptoError("log_rights")).code == "log_rights"
    assert hub_error(CryptoError("log_fork")).code == "header_invalid"
    assert hub_error(CryptoError("log_rollback")).code == "request_invalid"


def test_rebuilders_are_discovered_and_refuse_unknown_orgs(world: World) -> None:
    assert merge_mappings(PROJECTION_REBUILDERS)["orgs"] is REBUILDERS["orgs"]
    with world.hub.pool.connection() as connection, pytest.raises(LookupError):
        rebuild_org(connection, uuid.UUID(new_id()), world.hub.config.platform_root_key)


def test_append_runs_the_extra_step_in_the_same_transaction(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    wg = new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    seen: list[AppendResult] = []

    def extra(connection: Any, result: AppendResult) -> None:
        seen.append(result)
        raise HubError("id_conflict")

    with pytest.raises(HubError), world.hub.pool.connection() as connection, connection.transaction():
        append_batch(
            connection,
            org_id=uuid.UUID(draft.id),
            appender=admin.appender,
            expected_seq=1,
            entries=draft.pending(),
            wraps=draft.wraps(admin, "wg", wg, 1, [admin]),
            events=[],
            now=world.hub.clock(),
            root_key=world.hub.config.platform_root_key,
            extra=extra,
        )
    assert seen[0].head.seq == 1
    assert world.rows("SELECT log_head_seq FROM hub.org") == [(0,)]
    assert draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin])).status_code == 200


def test_malformed_entries_reach_the_format_check(world: World) -> None:
    draft = world.public_org()
    body = {"expected_seq": 1, "entries": ["not an entry"], "wraps": [], "events": []}
    response = draft.founder.browser.post(f"/v1/orgs/{draft.id}/log", json=body, extensions={"contract": False})
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/entries/0"]}}
    body = {"expected_seq": 1, "entries": [{"body": "x", "sig": "x"}], "wraps": [], "events": []}
    response = draft.founder.browser.post(f"/v1/orgs/{draft.id}/log", json=body, extensions={"contract": False})
    assert response.json() == {"error": "header_invalid"}
