"""Logs of test runs and work VM jobs at the hub fake as the hub serves them (`agent_hub.contexts.routes`
`get_task_log`, `get_task_log_chunk`; ADR-080 section 3): `GET /v1/tasks/{task}/logs/{object}` answers `{manifest,
sig, created_at, completed_at}` of a complete log of the task, `.../chunks/{index}` `{ct}`; a reader of the task's
workgroup only (`404` otherwise, also for a log of another task or an unknown chunk).

`add_log` seals output like the runner (`agent_runner.contexts.logs.store_log`): under the workgroup's current key,
signed by the device.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, cast

from agent_runner.crypto import runlog
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.kdf import SALT_BYTES
from agent_runner.crypto.log.state import WG

from ..hubfake import HubFake, Json, Request, User, now_ms_text
from .sessions import _right

UUID = r"[0-9a-f-]+"


@dataclass
class StoredLog:
    task: str
    manifest: dict[str, Any]
    sig: str
    chunks: list[bytes] = field(default_factory=list)


def logs_of(hub: HubFake) -> dict[str, StoredLog]:
    found = getattr(hub, "run_logs", None)
    if found is None:
        found = {}
        setattr(hub, "run_logs", found)  # noqa: B010
    return cast(dict[str, StoredLog], found)


def add_log(
    hub: HubFake,
    user: User,
    device: str,
    *,
    ref: str,
    output: bytes = b"",
    total: int | None = None,
    pieces: list[bytes] | None = None,
    changes: Json | None = None,
    place: dict[str, str],
) -> Json:
    """Seal `output` (or the chunk `pieces` as given) as the log of `ref` (place: wg, task, session) and store it;
    `changes` replace manifest fields before the device signs it. Returns the `log_ref` of the event."""
    wg = place["wg"]
    org = cast(str, hub.org_of_wg(wg))
    object_id = uuid7()
    salt = os.urandom(SALT_BYTES)
    key = runlog.log_key(hub.org_key(org, user, WG, wg), salt, object_id)
    parts = runlog.split(output) if pieces is None else pieces
    chunks = [runlog.seal_chunk(key, object_id, index, part) for index, part in enumerate(parts)]
    stored = sum(len(part) for part in parts)
    full = stored if total is None else total
    manifest: dict[str, Any] = {
        "v": runlog.VERSION,
        "kind": runlog.KIND,
        "ref": ref,
        "object": object_id,
        "org": org,
        "wg": wg,
        "task": place["task"],
        "session": place["session"],
        "author": device,
        "epoch": hub.orgs[org].log.state.epoch(WG, wg),
        "salt": b64u_encode(salt),
        "chunks": len(chunks),
        "size": sum(len(chunk) for chunk in chunks),
        "digest": runlog.digest(chunks),
        "bytes": full,
        "stored": stored,
        "ts": now_ms_text(),
        **(changes or {}),
    }
    sig = runlog.sign_manifest(hub.platform_devices[device].sig, manifest)
    logs_of(hub)[object_id] = StoredLog(place["task"], manifest, sig, chunks)
    return {"object_id": object_id, "bytes": full, "stored_bytes": stored, "truncated": stored < full}


def install(hub: HubFake) -> None:
    def stored(request: Request, task: str, object: str) -> StoredLog | None:
        found = logs_of(hub).get(object)
        row = hub.tasks.get(task)
        if found is None or row is None or found.task != task:
            return None
        return found if _right(hub, request, row["task"]["wg_id"], write=False) is None else None

    def described(request: Request, body: Any, task: str, object: str) -> tuple[int, Any]:
        found = stored(request, task, object)
        if found is None:
            return 404, {"error": "not_found"}
        at = now_ms_text()
        return 200, {"manifest": found.manifest, "sig": found.sig, "created_at": at, "completed_at": at}

    def chunk(request: Request, body: Any, task: str, object: str, index: str) -> tuple[int, Any]:
        found = stored(request, task, object)
        if found is None or int(index) >= len(found.chunks):
            return 404, {"error": "not_found"}
        return 200, {"ct": b64u_encode(found.chunks[int(index)])}

    hub.add_route("GET", rf"/v1/tasks/(?P<task>{UUID})/logs/(?P<object>{UUID})", described)
    hub.add_route("GET", rf"/v1/tasks/(?P<task>{UUID})/logs/(?P<object>{UUID})/chunks/(?P<index>[0-9]+)", chunk)
