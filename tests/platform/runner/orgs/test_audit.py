"""U-RUN-inbox / orgs: audit items and counters of refused inbound items (`agent_runner.orgs.audit`)."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.orgs import audit
from agent_runner.orgs.db import session

from ..reconcile.rig import Rig, pid

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")


def test_reject_and_log_alert_write_valid_audit_items(rig: Rig) -> None:
    async def body() -> None:
        async with rig.env() as env, session(env.pool) as conn:
            async with conn.transaction():
                await audit.reject(
                    env.context,
                    env.sync,
                    conn,
                    source="wrap",
                    code="wrap_unauthorized",
                    target_kind="wrap",
                    target_id="wg:x:1",
                )
                await audit.reject(
                    env.context,
                    env.sync,
                    conn,
                    source="event",
                    code="signature_invalid",
                    target_kind="event",
                    target_id="bad id!",
                )
                await audit.log_alert(
                    env.context,
                    env.sync,
                    conn,
                    code="log_rollback",
                    target_kind="org",
                    target_id=pid("org"),
                    verified_seq=7,
                    offered_seq=3,
                )
            items = await env.audits()
            for item in items:
                assert SCHEMAS.errors("outbox/audit.json", item) == []
            assert [item["action"] for item in items] == [
                "runner.inbound_rejected",
                "runner.inbound_rejected",
                "runner.alert",
                "runner.log_rollback",
            ]
            assert items[0]["detail"] == {"source": "wrap", "code": "wrap_unauthorized"}
            assert items[1]["target_id"] == "unknown"
            assert items[2]["detail"] == {"alert": "log_rollback", "value": None}
            assert items[3]["detail"] == {"verified_seq": 7, "offered_seq": 3}
            assert audit.counters(env.context) == Counter({"verify_failures": 2, "log_alerts": 1})
            assert audit.counters(env.context) is audit.counters(env.context)
            log = env.log()
            assert "runner.inbound rejected source=wrap code=wrap_unauthorized target=wrap:wg:x:1" in log
            assert f"runner.alert log_rollback org={pid('org')} verified_seq=7 offered_seq=3" in log
            async with conn.transaction():
                with pytest.raises(ValueError, match="source"):
                    await audit.reject(
                        env.context, env.sync, conn, source="mail", code="x", target_kind="x", target_id="x"
                    )
                with pytest.raises(ValueError, match="target kind"):
                    await audit.reject(
                        env.context, env.sync, conn, source="log", code="x", target_kind="X", target_id="x"
                    )

    asyncio.run(body())
