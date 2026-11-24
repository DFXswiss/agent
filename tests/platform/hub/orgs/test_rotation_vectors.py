"""U-HUB-log, rotation obligation: the hub computes R itself (`contracts/vectors/log-rotation.json`, CRY-02).

For every R case the hub refuses the triggering entry alone (`rotation_required`) unless R is empty or the member
leaves by itself (then R becomes `org.rotation_pending`), and accepts the entry with exactly one `rotate` per element of
R and the wraps of every new epoch. The batch vectors are accepted and the invalid batches refused with their codes.
A hub batch has one appender, which authors every entry and every wrap: the two vectors whose wraps are created by
another principal than the entries' author, and the vector whose entries have two authors, are refused earlier with
`forbidden` (decision HUB-05 2).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.crypto.encoding import parse_time
from agent_hub.errors import HubError
from agent_hub.log import vectors
from agent_hub.log.entries import entry_hash, sign_log_entry
from agent_hub.log.platform import verify_platform_log
from agent_hub.log.verify import verify_log
from agent_hub.orgs.append import append_batch
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.orgs.test_vectors import LABELS, _appender, _platform, _seed, _wraps_for, vector_hub

ROTATION = json.loads(
    (Path(__file__).parents[4] / "contracts" / "vectors" / "log-rotation.json").read_text(encoding="utf-8")
)
Json = dict[str, Any]
PLATFORM = verify_platform_log(ROTATION["logs"]["platform"], vectors.sig_pk("root")).state
__all__ = ["vector_hub"]
ONE_APPENDER = {"leave_without_rotation", "wrap_signed_by_other", "owed_rotation_by_other_member"}


def _submit(hub: HubHarness, by: str, entries: list[Json], wraps: list[Json], now: Any = None) -> None:
    with hub.pool.connection() as connection, connection.transaction():
        head = connection.execute("SELECT log_head_seq FROM hub.org").fetchone()
        append_batch(
            connection,
            org_id=uuid.UUID(vectors.ORG),
            appender=_appender(by),
            expected_seq=int(cast(tuple[int], head)[0]) + 1,
            entries=entries,
            wraps=wraps,
            events=[],
            now=now or hub.clock(),
            root_key=hub.config.platform_root_key,
        )


def _pending(hub: HubHarness) -> list[Json]:
    with hub.pool.connection() as connection:
        row = connection.execute("SELECT rotation_pending FROM hub.org").fetchone()
    return list(cast(tuple[list[Json]], row)[0])


def _key(item: Json) -> tuple[str, str]:
    return item["scope"], item["scope_id"]


@pytest.mark.parametrize("case", ROTATION["cases"], ids=[case["name"] for case in ROTATION["cases"]])
def test_hub_computes_the_rotate_set(vector_hub: HubHarness, case: Json) -> None:
    _platform(vector_hub, {"name": "platform", "count": 11})
    prefix = ROTATION["logs"][case["log"]["name"]][: case["log"]["count"]]
    _seed(vector_hub, len(prefix), private=True, log=prefix)
    entry = case["entry"]
    # an `agent_add` that replaces an agent of a lower generation owes the replaced agent's R in its batch (ADR-082
    # section 9.1): the vector names it as `lost` of the `losers`
    required = case["required"] or ([] if case.get("self_owed") or not case.get("losers") else case["lost"])
    body = entry["body"]
    # leaving and self-demotion owe their R to the others (`pending`, ADR-058)
    leave = body["type"] in ("remove", "role") and body.get("subject") == body["by"]
    if required and not leave:
        with pytest.raises(HubError) as refused:
            _submit(vector_hub, body["by"], [entry], [])
        assert refused.value.code == "rotation_required"
    states = verify_log([*prefix, entry], vectors.sig_pk("alice"), platform=PLATFORM).states
    batch = [entry]
    if not leave:
        for item in sorted(required, key=_key):
            seq = len(prefix) + len(batch)
            previous = batch[-1]
            rotate = {
                "org": vectors.ORG,
                "seq": seq,
                "prev": entry_hash(previous),
                "type": "rotate",
                "by": body["by"],
                "ts": body["ts"],
                "v": 1,
                "scope": item["scope"],
                "scope_id": item["scope_id"],
                "epoch": states[-1].epoch(item["scope"], item["scope_id"]) + 1,
            }
            batch.append(sign_log_entry(rotate, vectors.identity(LABELS[body["by"]]).sig))
    entries = [*prefix, *batch]
    states = verify_log(entries, vectors.sig_pk("alice"), platform=PLATFORM).states
    wraps = _wraps_for(states, (len(prefix), len(entries) - 1), entries=entries)
    _submit(vector_hub, body["by"], batch, wraps)
    if leave:
        assert sorted(map(_key, _pending(vector_hub))) == sorted(map(_key, required))


BATCHES = ROTATION["batches"] + ROTATION["invalid"]


@pytest.mark.parametrize("vector", BATCHES, ids=[vector["name"] for vector in BATCHES])
def test_rotation_batch_vectors(vector_hub: HubHarness, vector: Json) -> None:
    _platform(vector_hub, {"name": "platform", "count": 11})
    args = vector["args"]
    _seed(vector_hub, args["log"]["count"], private=True, log=ROTATION["logs"][args["log"]["name"]])
    now = parse_time(args["now"])
    if vector["name"] in ONE_APPENDER:
        assert {entry["body"]["by"] for entry in args["append"]} != {args["wrapped_by"]}
        assert any(entry["body"]["by"] != args["wrapped_by"] for entry in args["append"])
        with pytest.raises(HubError) as refused:
            _submit(vector_hub, args["wrapped_by"], args["append"], args["wraps"], now)
        assert refused.value.code == "forbidden"
        return
    if "error" in vector:
        with pytest.raises(HubError) as refused:
            _submit(vector_hub, args["wrapped_by"], args["append"], args["wraps"], now)
        assert refused.value.code == vector["error"]
        return
    _submit(vector_hub, args["wrapped_by"], args["append"], args["wraps"], now)
    assert sorted(map(_key, _pending(vector_hub))) == sorted(map(_key, vector["pending"]))
