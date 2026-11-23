"""`github.checkout` (05-runner.md section 12.1, 03-api.md section 9, ADR-015): the repository at a commit into the
session workspace, without the `git` binary and without running anything of the repository.

Pre-check (before the dedupe tag): `GET /repos/{r}` (access, and the default branch when `ref` is absent), then the
ref - branch, tag or (short) SHA - is resolved with `GET /repos/{r}/commits/{ref}` (`404`/`422` -> `not_found`). The
resolved SHA is the dedupe field `ref` and the executor's commit.

Executor: `GET /repos/{r}/tarball/{sha}` (one redirect to the codeload host, without the token) streamed into
`tmp/op-<id>/archive.tar.gz`, at most `ARCHIVE_LIMIT` (512 MiB, `size_exceeded`, counted by the writer so the
client's own bound never hides the code); `tarsafe.repack` reads it as an
untrusted archive (regular files and directories only - links and devices are skipped, absolute, `..` and `.git`
paths refuse the archive with `path_invalid`, at most 100 000 members and 512 MiB of content) and writes a plain tar
without the top directory; `files/import?dir=repos/<owner>/<name>&replace=true` puts it into the workspace (the guest
checks everything again, ATK-40). The plain tar is streamed from its scratch file in `CHUNK` pieces, so a checkout
never holds the archive in the runner's memory (decisions KOR-03, AUD-01 A-27). The directory uses the repository
name in lower case, as the allow-list does, so `github.commit` finds it whatever case the session writes.

Result `{repo, commit_sha, dir, files, bytes}` (the guest's counts). The base SHA per (session, repository) that
`github.commit` needs when its branch does not exist yet is this stored result (`base_sha`), so it survives restarts
without a table of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent_runner.github import access, ops, snapshot, tarsafe
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.guestlink.errors import GuestError
from agent_runner.operations.extensions import (
    CheckFailed,
    OperationContext,
    OperationFailed,
    PrecheckRequest,
    PrecheckResult,
)
from agent_runner.operations.rules import CHECKOUT_LIMITS, branch_valid

ARCHIVE_LIMIT = CHECKOUT_LIMITS["archive_bytes"]
CONTENT_LIMITS = tarsafe.Limits(max_bytes=ARCHIVE_LIMIT, max_entries=100_000)
TOO_LARGE = "The repository archive is larger than 512 MiB."
CHUNK = 1024 * 1024


def directory(repo: str) -> str:
    """The workspace directory of a repository: `repos/<owner>/<name>` in lower case (KOR-10, AUD-02 NB-04). GitHub
    names are case-insensitive and so are the allow-list and the dedupe of the runner, so every spelling of one
    repository shares one directory; the checkout result names it (`dir`), and `github.commit` exports it."""
    return f"repos/{repo.lower()}"


async def check(request: PrecheckRequest) -> PrecheckResult:
    repo = str(request.params["repo"])
    async with access.client(request) as github:
        info = await access.repository(github, repo)
        ref = request.params.get("ref")
        if ref is None:
            ref = info.get("default_branch")
            if not branch_valid(ref):
                raise ops.invalid()
        try:
            answer = await github.request("GET", f"/repos/{repo}/commits/{quote(str(ref), safe='/')}")
        except GitHubError as error:
            if error.kind in ("not_found", "unprocessable"):
                raise CheckFailed("not_found", message="The ref does not exist in the repository.") from None
            raise
        sha = ops.text(ops.obj(answer.data).get("sha"), ops.SHA_RE)
    return PrecheckResult(dedupe={"ref": sha}, data={"commit_sha": sha})


async def base_sha(conn: Any, session_id: str, repo: str) -> str | None:
    """The commit of the latest succeeded checkout of `repo` in the session (the base of a new branch)."""
    cur = await conn.execute(
        "SELECT result->>'commit_sha' FROM runner.operation WHERE session_id = %s AND type = 'github.checkout'"
        " AND state = 'succeeded' AND lower(result->>'repo') = lower(%s) ORDER BY created_at DESC, id DESC LIMIT 1",
        (session_id, repo),
    )
    row = await cur.fetchone()
    return None if row is None else str(row[0])


def _repack(scratch: Any) -> tarsafe.Stats:
    with (scratch / "archive.tar.gz").open("rb") as source, (scratch / "import.tar").open("wb") as target:
        return tarsafe.repack(source, target, limits=CONTENT_LIMITS)


async def chunks(path: Path) -> AsyncGenerator[bytes]:
    """The file's bytes in pieces of at most `CHUNK`, read in a worker thread."""
    with path.open("rb") as source:
        while chunk := await asyncio.to_thread(source.read, CHUNK):
            yield chunk


async def _import(ctx: OperationContext, target: str, packed: Path) -> dict[str, Any]:
    try:
        guest = await snapshot.guest_for(ctx.env, ctx.session)
    except snapshot.NoGuest:
        raise OperationFailed("not_found", message="The session has no VM on this device.") from None
    length = packed.stat().st_size
    try:
        async with guest, contextlib.aclosing(chunks(packed)) as stream:
            answer = await guest.import_files(ctx.session.id, target, stream, replace=True, size=length)
    except GuestError as error:
        code, retryable = snapshot.guest_code(error)
        raise OperationFailed(code, retryable=retryable, message="The workspace import failed.") from None
    files, size = answer.get("files"), answer.get("bytes")
    if type(files) is not int or type(size) is not int or files < 0 or size < 0:
        raise OperationFailed("upstream_error", message="The workspace import answered unexpectedly.")
    return {"files": files, "bytes": size}


async def checkout(ctx: OperationContext, github: GitHubClient) -> dict[str, Any]:
    repo = str(ctx.params["repo"])
    sha = ops.text(ctx.data.get("commit_sha"), ops.SHA_RE)
    snapshot.sweep(ctx.context)
    scratch = snapshot.fresh(ctx.context, ctx.operation.id)
    try:
        with (scratch / "archive.tar.gz").open("wb") as out:
            bounded = snapshot.Bounded(out, ARCHIVE_LIMIT)
            await github.download(f"/repos/{repo}/tarball/{sha}", bounded.write, max_bytes=sys.maxsize)
        stats = await asyncio.to_thread(_repack, scratch)
        packed = scratch / "import.tar"
        if packed.stat().st_size > ARCHIVE_LIMIT:
            raise snapshot.TooLarge
        target = directory(repo)
        imported = await _import(ctx, target, packed)
    except snapshot.TooLarge:
        raise OperationFailed("size_exceeded", message=TOO_LARGE) from None
    except tarsafe.UnsafeArchive as error:
        code = "upstream_error" if error.code == "archive_invalid" else error.code
        raise OperationFailed(code, message="The repository archive was refused.") from None
    finally:
        snapshot.remove(ctx.context, ctx.operation.id)
    ctx.env.log(f"runner.github checkout operation_id={ctx.operation.id} files={stats.files} skipped={stats.skipped}")
    return {"repo": repo, "commit_sha": sha, "dir": target, **imported}


execute = ops.executor(checkout)
