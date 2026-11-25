"""Registration of pull requests at the hub (ADR-072 section 8, ADR-084 section 1; decisions/KOR-33.md).

After a `github.create_pull_request` operation of this device succeeded, the hub learns an opaque relation pull
request -> task: `POST /v1/device/pr-links {session_id, epoch, tag, sealed}` (the hub knows the session's task and
workgroup) with `tag = crypto.context.pr_link_tag(WK_e, repo, number)` under the workgroup's current epoch and the
sealed body `crypto.context.seal_pr_link` under the same `WK_e`, bound to the workgroup, epoch, tag, the session's
task and the origin `device:<session_id>` (04-crypto.md section 4.11a). Repository and number never leave the device
in clear. An older hub refuses the unknown field (`request_invalid` whose pointers name `/sealed`); the registration
is then sent once more without it, so the link exists ("details unavailable") instead of failing.

The operations are read from `runner.operation` (`store.pr_link_work`), not from the single `op_finished` notifier of
RUN-14 (which pings sessions and is only called for answers that came after the MCP call returned `pending`): every
succeeded operation is registered exactly once, also after a restart, a hub outage or an operation that finished in
time. An accepted registration is `done`; a transport failure, `not_found` (a hub without the endpoint) or
`unavailable`/`rate_limited` is retried with backoff; any other refusal is `refused` (log line).
"""

from __future__ import annotations

from datetime import timedelta

from agent_runner.contexts import store
from agent_runner.contexts.checkpoint import RETRY_CODES, current_key, retry_delay
from agent_runner.contexts.env import ContextEnv
from agent_runner.contexts.store import PullRequest
from agent_runner.crypto import context as crypto
from agent_runner.crypto.errors import CryptoError
from agent_runner.sync.http import HubRefusal, HubTransportError

TARGET = "/v1/device/pr-links"


async def register(env: ContextEnv, pull: PullRequest) -> str:
    """`done`, `retry` or `refused`."""
    async with env.sessions.transaction() as conn:
        key = await current_key(conn, env.sessions.storage_key, pull.wg_id)
    if key is None:
        return await _retry(env, pull, "key_missing")
    epoch, workgroup_key = key
    try:
        tag = crypto.pr_link_tag(crypto.pr_link_key(workgroup_key), pull.repo, pull.number)
        sealed = crypto.seal_pr_link(
            crypto.pr_link_body_key(workgroup_key),
            wg_id=pull.wg_id,
            epoch=epoch,
            tag=tag,
            task_id=pull.task_id,
            origin=f"device:{pull.session_id}",
            repo=pull.repo,
            number=pull.number,
        )
    except CryptoError:
        return await _state(env, pull, "refused", "pr_link_refused code=params_invalid")
    body = {"session_id": pull.session_id, "epoch": epoch, "tag": tag}
    try:
        await _post(env, body, sealed)
    except HubTransportError as failed:
        return await _retry(env, pull, failed.describe())
    except HubRefusal as refused:
        if refused.code in RETRY_CODES:
            return await _retry(env, pull, f"code={refused.code}")
        return await _state(env, pull, "refused", f"pr_link_refused code={refused.code}")
    return await _state(env, pull, "done", "pr_link_registered")


async def _post(env: ContextEnv, body: dict[str, object], sealed: str) -> None:
    """The registration with its sealed body; once more without it when an older hub refuses the field."""
    try:
        await env.hub.request("POST", TARGET, {**body, "sealed": sealed})
    except HubRefusal as refused:
        pointers = refused.detail.get("pointers")
        if refused.code != "request_invalid" or not isinstance(pointers, list) or "/sealed" not in pointers:
            raise
        await env.hub.request("POST", TARGET, body)


async def _state(env: ContextEnv, pull: PullRequest, state: str, line: str) -> str:
    async with env.sessions.transaction() as conn:
        await store.pr_link_state(conn, pull.operation_id, state)
    env.line(f"{line} operation_id={pull.operation_id}")
    return state


async def _retry(env: ContextEnv, pull: PullRequest, why: str) -> str:
    next_at = env.now() + timedelta(seconds=retry_delay(pull.attempts))
    async with env.sessions.transaction() as conn:
        await store.pr_link_state(conn, pull.operation_id, "pending", next_at=next_at)
    env.line(f"pr_link_retry operation_id={pull.operation_id} {why}")
    return "retry"


async def round(env: ContextEnv) -> int:
    async with env.sessions.transaction() as conn:
        work = await store.pr_link_work(conn, env.now())
    for pull in work:
        await register(env, pull)
    return len(work)
