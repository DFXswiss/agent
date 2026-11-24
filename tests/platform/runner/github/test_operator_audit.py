"""U-RUN-github (KOR-47, ADR-078 section 7): every change of `OPERATOR_GITHUB_USERS` is audited once
(`runner.operator_github_users {count, added, removed}`, outbox item `audit`), an unchanged list never; the store keeps
keyed hashes of the user IDs only. Real PostgreSQL and the hub kit's daemon context."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_guest.schema import SchemaSet
from agent_runner.github import operator_audit, tasks

from ..credentials.kit import ALICE, BOB, Env, kit_env, pid

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")
CAROL = pid("carol")


async def changes(env: Env) -> list[Any]:
    found = [item for item in await env.audits() if item["action"] == operator_audit.ACTION]
    for item in found:
        assert SCHEMAS.errors("outbox/audit.json", item) == []
        assert (item["result"], item["target_kind"]) == ("ok", "device")
        assert item["target_id"] == item_device(env)
    return [item["detail"] for item in found]


def item_device(env: Env) -> str:
    return str(env.context.identity.device.device_id)


def test_each_change_of_the_list_is_audited_once(store_dsn: str) -> None:
    async def main() -> None:
        async with kit_env(store_dsn) as env:
            config = env.context.config
            await operator_audit.audit(env.context)  # an empty list on a fresh store: nothing to audit
            assert await changes(env) == []
            config.operator_github_users = frozenset({ALICE, BOB})
            await operator_audit.audit(env.context)
            await operator_audit.audit(env.context)  # the next start with the same list
            assert await changes(env) == [{"count": 2, "added": 2, "removed": 0}]
            stored = [row[0] for row in await env.rows("SELECT user_hmac FROM runner.operator_github_user")]
            assert len(stored) == 2
            assert not {ALICE, BOB, ALICE.replace("-", "")} & set(stored)
            config.operator_github_users = frozenset({ALICE, CAROL})
            await operator_audit.audit(env.context)
            config.operator_github_users = frozenset()
            await operator_audit.audit(env.context)
            assert await changes(env) == [
                {"count": 2, "added": 2, "removed": 0},
                {"count": 2, "added": 1, "removed": 1},
                {"count": 0, "added": 0, "removed": 2},
            ]
            assert await env.rows("SELECT user_hmac FROM runner.operator_github_user") == []
            log = env.log()
            assert "github_operator_users changed count=2 added=1 removed=1\n" in log
            assert ALICE not in log
            assert BOB not in log

    asyncio.run(main())


def test_an_unenrolled_device_audits_nothing() -> None:
    asyncio.run(operator_audit.audit(SimpleNamespace(identity=None)))


def test_the_area_registers_the_task() -> None:
    assert [(spec.name, spec.run) for spec in tasks.TASKS] == [("github_operator_users", operator_audit.audit)]
