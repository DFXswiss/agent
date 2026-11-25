"""`github.merge_base` (ADR-084 section 4, 03-api.md section 9, 05-runner.md section 12.1): the base of a pull request
merged into its head in the session workspace, with conflict markers where both sides changed the same lines. No
commit, no ref update; the merge commit is `github.commit` with `merge: true`.

Pre-check (after the identity resolution of ADR-078; no dedupe slot - every call prepares anew):

1. `GET /repos/{r}` (access, write); `GET /repos/{r}/pulls/{n}` (`404` -> `not_found`): open, not merged, and the head
   in the same repository - a closed or merged pull request or a head in another repository (a fork) is
   `merge_unsupported`; the head branch must not be protected by the workgroup settings (`branch_protected`);
2. the **base tip** `GET /repos/{r}/git/ref/heads/{base}` (GitHub's `base.sha` of the pull request can lag behind;
   missing -> `not_found`) and the merge base `GET /repos/{r}/compare/{tip}...{head}` (`merge_base_commit`; no
   common history -> `merge_unsupported`); `up_to_date` iff the merge base is the tip - nothing else happens then;
3. **workspace protection** (unless `discard`): one export of `repos/<owner>/<name>` (a missing or empty directory is
   clean); anything the export skipped (a link, a special file, a protected name the session wrote) is
   `workspace_dirty`; the regular files must equal the head's tree (regular files without protected names) or the digest
   the runner stored for the directory (`merge_prep.remembered`: the last checkout or preparation, or the snapshot of a
   succeeded commit - then only while the head or the branch it was committed to contains that commit, else
   `workspace_dirty`, push first); anything else is `workspace_dirty` naming up to 20 paths. The message tells the
   session to replace changes only after the person agreed.

Executor: the trees of the merge base, the head and the base tip (`GET /git/commits/{sha}`, `GET
/git/trees/{tree}?recursive=1`; a truncated listing is `size_exceeded`), merged by `merge3.merge_entries`. The contents
the result needs - every regular file of the result outside protected names, and the three versions of every file
changed on both sides - come from the head's tarball, then the base's (each archive at most 512 MiB, read as untrusted
through `tarsafe`), **each member checked against its blob SHA**; a member that is missing (protected names,
`export-ignore`) or differs (`export-subst`) is read with `GET /git/blobs/{sha}` and checked as well (at most
`MAX_BLOB_READS`, else `size_exceeded`). Contents are stored once per blob SHA in the scratch directory. Files changed
on both sides: `merge3.merge_text` for text (clean -> the merged version; conflict -> the version with markers,
`content`, or for a protected name the head's version, `protected`), the head's version for anything else (`binary`,
`protected` for a protected name). The merged regular files without protected names are written as one plain tar
(files `0600`, directories `0700`) and imported into `repos/<owner>/<name>` with `replace=true` (the directory and the
import of `github.checkout`).

Then the preparation is stored (`merge_prep.save`): every path whose result differs from the head, every conflict, the
head's and base's entries of the conflicts without markers and the marker-like lines the head's and base's versions of
every `content` conflict already contain; and the digest of the imported files. A protected text merged at blob level is
uploaded as a blob (`POST /git/blobs`; the merge commit refers to it, the workspace never holds it). Result `{repo,
number, branch, base, head_sha, base_sha, merge_base_sha, up_to_date, conflicts (at most 1 000, by path),
conflicts_total, dir, files, bytes}`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import os
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent_runner.github import access, checkout, gitobj, merge3, merge_prep, ops, snapshot, tarsafe
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.guestlink.errors import GuestError, GuestRefused
from agent_runner.operations.extensions import (
    CheckFailed,
    OperationContext,
    OperationFailed,
    PrecheckRequest,
    PrecheckResult,
)
from agent_runner.operations.rules import branch_valid, protected
from agent_runner.orgs.db import session as store_session

MAX_CONFLICTS_LISTED = 1000
MAX_PATHS_NAMED = 20
MAX_BLOB_READS = 1000
NOT_OPEN = "The pull request is not open."
FORK = "The head branch of the pull request is in another repository (a fork); fixing its conflicts is not supported."
NO_HISTORY = "The head and the base of the pull request have no common history."
MISSING_BASE = "The base branch of the pull request does not exist."
TOO_MANY_BLOBS = "The merge needs more single file reads than allowed."
TREE_TOO_LARGE = "A tree of the merge is too large to list."
WHAT_TO_DO = (
    " Commit and push them first. Replace them only when the person who gave the instruction agreed (ask with"
    " human.question), then call github.merge_base with discard: true."
)
UNPUSHED = "The workspace holds a commit that is not pushed yet: push it first."
Files = dict[str, tuple[str, str]]


def _dirty(paths: list[str]) -> CheckFailed:
    named = ", ".join(paths[:MAX_PATHS_NAMED])
    more = f" and {len(paths) - MAX_PATHS_NAMED} more" if len(paths) > MAX_PATHS_NAMED else ""
    return CheckFailed(
        "workspace_dirty",
        message=f"The workspace directory has changes that are not committed: {named}{more}." + WHAT_TO_DO,
    )


async def _pull(github: GitHubClient, repo: str, number: int) -> dict[str, Any]:
    try:
        answer = await github.request("GET", f"/repos/{repo}/pulls/{number}")
    except GitHubError as error:
        if error.kind == "not_found":
            raise CheckFailed("not_found", message="The pull request does not exist.") from None
        raise
    data = ops.obj(answer.data)
    head, base = ops.obj(data.get("head")), ops.obj(data.get("base"))
    if data.get("state") != "open" or data.get("merged") is True:
        raise CheckFailed("merge_unsupported", message=NOT_OPEN)
    head_repo = head.get("repo")
    name = ops.obj(head_repo).get("full_name") if head_repo is not None else None
    if not isinstance(name, str) or name.lower() != repo.lower():
        raise CheckFailed("merge_unsupported", message=FORK)
    refs = (head.get("ref"), base.get("ref"))
    if not all(branch_valid(ref) for ref in refs):
        raise ops.invalid()
    return {"head_ref": str(refs[0]), "head_sha": ops.text(head.get("sha"), ops.SHA_RE), "base_ref": str(refs[1])}


async def _tip(github: GitHubClient, repo: str, branch: str) -> str:
    try:
        answer = await github.request("GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}")
    except GitHubError as error:
        if error.kind == "not_found":
            raise CheckFailed("not_found", message=MISSING_BASE) from None
        raise
    return ops.text(ops.obj(ops.obj(answer.data).get("object")).get("sha"), ops.SHA_RE)


async def _merge_base(github: GitHubClient, repo: str, base: str, head: str) -> str:
    try:
        answer = await github.request("GET", f"/repos/{repo}/compare/{base}...{head}")
    except GitHubError as error:
        if error.kind == "not_found":
            raise CheckFailed("merge_unsupported", message=NO_HISTORY) from None
        raise
    return ops.text(ops.obj(ops.obj(answer.data).get("merge_base_commit")).get("sha"), ops.SHA_RE)


async def tree_files(github: GitHubClient, repo: str, commit: str) -> Files:
    """The leaf entries of a commit's tree (`size_exceeded` when GitHub truncates the listing)."""
    answer = await github.request("GET", f"/repos/{repo}/git/commits/{commit}")
    tree = ops.text(ops.obj(ops.obj(answer.data).get("tree")).get("sha"), ops.SHA_RE)
    answer = await github.request("GET", f"/repos/{repo}/git/trees/{tree}", params={"recursive": "1"})
    listing = ops.obj(answer.data)
    if listing.get("truncated") is True:
        raise OperationFailed("size_exceeded", message=TREE_TOO_LARGE)
    items = listing.get("tree")
    try:
        return gitobj.flatten(items if isinstance(items, list) else [None])
    except ValueError:
        raise ops.invalid() from None


def workspace_view(files: Mapping[str, tuple[str, str]]) -> dict[str, str]:
    """`path -> blob` of the regular files a checkout puts into the workspace (no links, no protected names)."""
    return {
        path: sha for path, (mode, sha) in files.items() if mode in gitobj.REGULAR_MODES and not tarsafe.protected(path)
    }


async def _exported(request: PrecheckRequest, repo: str) -> tuple[dict[str, str], list[str]]:
    """`(path -> blob, skipped paths)` of the repository directory in the workspace (empty when it does not exist);
    skipped are links, special and unreadable files and protected names the session created there."""
    context, op_id = request.context, request.operation.id
    snapshot.sweep(context)
    scratch = snapshot.fresh(context, op_id)
    try:
        guest = await snapshot.guest_for(snapshot.env_of(context), request.session)
        async with guest:
            snap = await snapshot.take(guest, request.session.id, checkout.directory(repo), None, scratch)
    except snapshot.NoGuest:
        raise CheckFailed("not_found", message="The session has no VM on this device.") from None
    except GuestRefused as error:
        if error.error == "not_found":
            return {}, []
        raise CheckFailed(snapshot.guest_code(error)[0], message="The workspace export failed.") from None
    except GuestError as error:
        raise CheckFailed(snapshot.guest_code(error)[0], message="The workspace export failed.") from None
    except snapshot.TooLarge:
        raise CheckFailed("size_exceeded", message="The workspace export is too large.") from None
    except snapshot.SnapshotInvalid:
        raise CheckFailed("upstream_error", message="The workspace export was not valid.") from None
    finally:
        snapshot.remove(context, op_id)
    files = {path: found.blob for path, found in snap.files.items() if not tarsafe.protected(path)}
    skipped = [str(item["path"]) for item in snap.skipped] + sorted(set(snap.files) - set(files))
    return files, skipped


async def _contains(github: GitHubClient, repo: str, head: str, commit: str) -> bool:
    """Whether `commit` is the head or an ancestor of it."""
    try:
        answer = await github.request("GET", f"/repos/{repo}/compare/{commit}...{head}")
    except GitHubError as error:
        if error.kind == "not_found":
            return False
        raise
    return ops.obj(answer.data).get("status") in ("ahead", "identical")


async def _pushed(github: GitHubClient, repo: str, head: str, known: merge_prep.Known) -> bool:
    """A committed snapshot is safe to replace once its commit is on the pull request's head or on the branch it was
    committed to (pushed there)."""
    commit = str(known.commit)
    if await _contains(github, repo, head, commit):
        return True
    return known.branch is not None and await _contains(github, repo, quote(known.branch, safe="/"), commit)


async def _protect(request: PrecheckRequest, github: GitHubClient, repo: str, pull: Mapping[str, str]) -> None:
    """The workspace protection (module docstring, step 3)."""
    files, skipped = await _exported(request, repo)
    if skipped:
        raise _dirty(sorted(skipped))
    if not files:
        return
    try:
        view = workspace_view(await tree_files(github, repo, pull["head_sha"]))
    except OperationFailed as failed:
        raise CheckFailed(failed.code, message=failed.message) from None
    current = merge_prep.digest(files.items())
    if current == merge_prep.digest(view.items()):
        return
    async with store_session(request.context.pool) as conn:
        known = await merge_prep.remembered(conn, request.session.id, checkout.directory(repo))
    if known is not None and current == known.digest:
        if known.commit is None or await _pushed(github, repo, pull["head_sha"], known):
            return
        raise CheckFailed("workspace_dirty", message=UNPUSHED)
    raise _dirty(sorted(path for path in set(files) | set(view) if files.get(path) != view.get(path)))


async def check(request: PrecheckRequest) -> PrecheckResult:
    repo, number = str(request.params["repo"]), int(request.params["number"])
    async with access.client(request) as github:
        await access.repository(github, repo, write=True)
        pull = await _pull(github, repo, number)
        if protected(pull["head_ref"], request.settings.protected_branches):
            raise CheckFailed("branch_protected", message="The head branch of the pull request is protected.")
        tip = await _tip(github, repo, pull["base_ref"])
        merge_base = await _merge_base(github, repo, tip, pull["head_sha"])
        up_to_date = merge_base == tip
        if not up_to_date and not request.params.get("discard", False):
            await _protect(request, github, repo, pull)
    data = {**pull, "base_sha": tip, "merge_base_sha": merge_base, "up_to_date": up_to_date}
    return PrecheckResult(data=data)


# -- executor ------------------------------------------------------------------------------------------------------


@dataclass
class Store:
    """Contents by blob SHA in `<scratch>/blobs` (each written once, always checked against its SHA)."""

    root: Path
    known: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.root.mkdir(mode=0o700, exist_ok=True)

    def path(self, sha: str) -> Path:
        return self.root / sha

    def put(self, data: bytes) -> str:
        sha = gitobj.blob_sha(data)
        if sha not in self.known:
            self.path(sha).write_bytes(data)
            self.known.add(sha)
        return sha

    def read(self, sha: str) -> bytes:
        return self.path(sha).read_bytes()

    def size(self, sha: str) -> int:
        return self.path(sha).stat().st_size


def _take(store: Store, plain: Path, wanted: set[str]) -> None:
    """Store every member of a plain tar whose content has a wanted blob SHA (checked while it is copied)."""
    with plain.open("rb") as source:
        for entry in tarsafe.entries(source, limits=checkout.CONTENT_LIMITS, links="refuse", compressed=False):
            if entry.reader is None:
                continue
            temporary = store.root / ".incoming"
            with temporary.open("wb") as out:
                sha = _copy(entry.reader, entry.size, out)
            if sha in wanted and sha not in store.known:
                os.replace(temporary, store.path(sha))
                store.known.add(sha)
            else:
                temporary.unlink()


def _copy(reader: Any, size: int, out: Any) -> str:
    digest = hashlib.sha1(f"blob {size}\0".encode("ascii"), usedforsecurity=False)

    def write(chunk: bytes) -> None:
        digest.update(chunk)
        out.write(chunk)

    tarsafe.copy_exact(reader, size, write)
    return digest.hexdigest()


async def _archive(github: GitHubClient, repo: str, sha: str, scratch: Path) -> Path:
    """The tarball of a commit as a plain tar in the scratch directory."""
    packed, plain = scratch / "archive.tar.gz", scratch / f"{sha}.tar"
    with packed.open("wb") as out:
        bounded = snapshot.Bounded(out, checkout.ARCHIVE_LIMIT)
        await github.download(f"/repos/{repo}/tarball/{sha}", bounded.write, max_bytes=sys.maxsize)

    def repack() -> None:
        with packed.open("rb") as source, plain.open("wb") as target:
            tarsafe.repack(source, target, limits=checkout.CONTENT_LIMITS)
        packed.unlink()

    await asyncio.to_thread(repack)
    return plain


async def blob(github: GitHubClient, repo: str, sha: str) -> bytes:
    answer = await github.request("GET", f"/repos/{repo}/git/blobs/{sha}")
    data = ops.obj(answer.data)
    try:
        content = (
            base64.b64decode(str(data.get("content", "")), validate=False) if data.get("encoding") == "base64" else None
        )
    except binascii.Error:
        content = None
    if content is None or gitobj.blob_sha(content) != sha:
        raise ops.invalid()
    return content


@dataclass
class Outcome:
    result: dict[str, tuple[str, str]]
    conflicts: dict[str, str]
    sides: dict[str, dict[str, list[str] | None]]
    markers: dict[str, list[str]]


def _entry(value: tuple[str, str] | None) -> list[str] | None:
    return None if value is None else [value[0], value[1]]


def _mode(o: tuple[str, str] | None, a: tuple[str, str], b: tuple[str, str]) -> str:
    return b[0] if o is not None and a[0] == o[0] else a[0]


def resolve(plan: merge3.Plan, o: Files, a: Files, b: Files, *, store: Store, labels: merge3.Labels) -> Outcome:
    """The line merges of `plan.content` and the sides of every conflict without markers (module docstring)."""
    out = Outcome(dict(plan.result), dict(plan.conflicts), {}, {})
    for path in plan.content:
        ours, theirs, base = a[path], b[path], o.get(path)
        hidden = tarsafe.protected(path)
        texts = [sha for sha in (ours[1], theirs[1], None if base is None else base[1]) if sha is not None]
        if all(store.size(sha) <= merge3.TEXT_LIMIT and merge3.is_text(store.read(sha)) for sha in texts):
            merged, conflicted = merge3.merge_text(
                None if base is None else store.read(base[1]), store.read(ours[1]), store.read(theirs[1]), labels
            )
            if not conflicted:
                out.result[path] = (_mode(base, ours, theirs), store.put(merged))
                continue
            if not hidden:
                out.result[path] = (_mode(base, ours, theirs), store.put(merged))
                out.conflicts[path] = "content"
                known = merge3.marker_lines(store.read(ours[1])) | merge3.marker_lines(store.read(theirs[1]))
                out.markers[path] = sorted(line.decode("latin-1") for line in known)
                continue
        out.conflicts[path] = "protected" if hidden else "binary"
    for path, kind in out.conflicts.items():
        if kind != "content":
            out.sides[path] = {"head": _entry(a.get(path)), "base": _entry(b.get(path))}
    return out


def _needed(plan: merge3.Plan, o: Files, a: Files, b: Files) -> set[str]:
    wanted = set(workspace_view(plan.result).values())
    for path in plan.content:
        wanted |= {a[path][1], b[path][1]}
        if path in o:
            wanted.add(o[path][1])
    return wanted


def write_import(target: Path, files: Mapping[str, str], store: Store) -> None:
    """The plain tar of the merged workspace files (`path -> blob`): directories first, files `0600`."""
    directories = sorted(
        {"/".join(path.split("/")[:depth]) for path in files for depth in range(1, path.count("/") + 1)}
    )
    with target.open("wb") as raw, tarfile.open(fileobj=raw, mode="w|", format=tarfile.PAX_FORMAT) as out:
        for name in directories:
            out.addfile(tarsafe.member_info(name, "dir", 0))
        for path in sorted(files):
            data = store.read(files[path])
            out.addfile(tarsafe.member_info(path, "file", len(data)), io.BytesIO(data))


async def _contents(
    github: GitHubClient, repo: str, data: Mapping[str, Any], *, wanted: set[str], store: Store
) -> None:
    for sha in (data["head_sha"], data["base_sha"]):
        if wanted <= store.known:
            break
        plain = await _archive(github, repo, sha, store.root.parent)
        await asyncio.to_thread(_take, store, plain, wanted)
        plain.unlink()
    missing = sorted(wanted - store.known)
    if len(missing) > MAX_BLOB_READS:
        raise OperationFailed("size_exceeded", message=TOO_MANY_BLOBS)
    for sha in missing:
        content = await blob(github, repo, sha)
        store.put(content)


async def _upload_hidden(
    github: GitHubClient, repo: str, result: Mapping[str, tuple[str, str]], trees: tuple[Files, Files], store: Store
) -> None:
    """Upload the merged blobs of entries the workspace never holds (a protected text merged at blob level): the
    merge commit takes them from the preparation, not from the workspace (`merge_commit`)."""
    known = {sha for tree in trees for _, sha in tree.values()}
    for path, (_, sha) in sorted(result.items()):
        if tarsafe.protected(path) and sha not in known:
            body = {"content": base64.b64encode(store.read(sha)).decode("ascii"), "encoding": "base64"}
            answer = await github.request("POST", f"/repos/{repo}/git/blobs", body=body)
            if ops.obj(answer.data).get("sha") != sha:
                raise OperationFailed("tree_mismatch", message="GitHub stored a blob with another SHA.")


def _result(
    params: Mapping[str, Any], data: Mapping[str, Any], conflicts: Mapping[str, str], imported: Mapping[str, int]
) -> dict[str, Any]:
    listed = [{"path": path, "kind": conflicts[path]} for path in sorted(conflicts)[:MAX_CONFLICTS_LISTED]]
    return {
        "repo": str(params["repo"]),
        "number": int(params["number"]),
        "branch": data["head_ref"],
        "base": data["base_ref"],
        "head_sha": data["head_sha"],
        "base_sha": data["base_sha"],
        "merge_base_sha": data["merge_base_sha"],
        "up_to_date": bool(data["up_to_date"]),
        "conflicts": listed,
        "conflicts_total": len(conflicts),
        "dir": checkout.directory(str(params["repo"])),
        "files": imported["files"],
        "bytes": imported["bytes"],
    }


async def merge_base(ctx: OperationContext, github: GitHubClient) -> dict[str, Any]:
    params, data = ctx.params, ctx.data
    repo = str(params["repo"])
    if data["up_to_date"]:
        return _result(params, data, {}, {"files": 0, "bytes": 0})
    o = await tree_files(github, repo, data["merge_base_sha"])
    a = await tree_files(github, repo, data["head_sha"])
    b = await tree_files(github, repo, data["base_sha"])
    plan = await asyncio.to_thread(merge3.merge_entries, o, a, b)
    snapshot.sweep(ctx.context)
    scratch = snapshot.fresh(ctx.context, ctx.operation.id)
    try:
        store = Store(scratch / "blobs")
        await _contents(github, repo, data, wanted=_needed(plan, o, a, b), store=store)
        labels = merge3.Labels(data["head_ref"], data["base_ref"])
        outcome = await asyncio.to_thread(lambda: resolve(plan, o, a, b, store=store, labels=labels))
        files = workspace_view(outcome.result)
        packed = scratch / "import.tar"
        await asyncio.to_thread(write_import, packed, files, store)  # the guest's import limits apply
        await _upload_hidden(github, repo, outcome.result, (a, b), store)
        imported = await checkout.import_tar(ctx, checkout.directory(repo), packed)
    except tarsafe.UnsafeArchive as error:
        code = "upstream_error" if error.code == "archive_invalid" else error.code
        raise OperationFailed(code, message="The repository archive was refused.") from None
    except snapshot.TooLarge:
        raise OperationFailed("size_exceeded", message=checkout.TOO_LARGE) from None
    finally:
        snapshot.remove(ctx.context, ctx.operation.id)
    changed = {
        path: _entry(outcome.result.get(path))
        for path in set(outcome.result) | set(a)
        if outcome.result.get(path) != a.get(path)
    }
    prep = merge_prep.Prep(
        ctx.operation.id,
        ctx.session.id,
        repo,
        int(params["number"]),
        data["head_ref"],
        data["head_sha"],
        data["base_sha"],
        data["merge_base_sha"],
        changed,
        outcome.conflicts,
        outcome.sides,
        outcome.markers,
        merge_prep.PENDING,
        None,
    )
    async with store_session(ctx.context.pool) as conn:
        await merge_prep.save(conn, prep)
        await merge_prep.remember(
            conn, ctx.session.id, checkout.directory(repo), merge_prep.Known(merge_prep.digest(files.items()))
        )
    ctx.env.log(
        f"runner.github merge_base operation_id={ctx.operation.id} conflicts={len(outcome.conflicts)}"
        f" changed={len(changed)}"
    )
    return _result(params, data, outcome.conflicts, imported)


execute = ops.executor(merge_base)
