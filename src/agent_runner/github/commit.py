"""`github.commit` (05-runner.md sections 12 step 3 and 12.1, 03-api.md section 9, ADR-015): a commit of exactly one
workspace snapshot through the Git Data API, with blob and tree SHAs computed locally; no ref update.

Pre-check (before the dedupe tag, so the snapshot decides the dedupe fields):

1. `GET /repos/{r}` (access); the parent = the head of `branch` at GitHub now (`GET /repos/{r}/git/ref/heads/{b}`),
   or - when the branch does not exist - the commit of the session's latest checkout of the repository
   (`checkout.base_sha`; none -> `not_found`); `GET /repos/{r}/git/commits/{parent}` -> its tree;
   `GET /repos/{r}/git/trees/{tree}?recursive=1` -> the parent's files (a truncated listing is `size_exceeded`);
2. **one** export snapshot of `repos/<owner>/<name>` (the commit's `paths` below it) into `tmp/op-<id>`
   (`snapshot.take`);
3. the new tree (`plan`): without `paths` the snapshot is the complete set of regular files - every regular file of the
   parent missing in it is deleted; with `paths` only files at or below a listed path are compared. Executable bits of
   existing files are kept (`100755`), new files are `100644`. Symbolic links and submodules of the parent stay as they
   are (the checkout never creates them, so their absence in the workspace is no deletion), and so does every parent
   entry at or below a path the export skipped (a link, a special or unreadable file is no deletion either); a new
   file replaces a link, submodule or directory at its path. Protected names (`tarsafe.protected`: `.claude/`,
   `.mcp.json`, `CLAUDE.md`, …) are never touched: the checkout never imports them, so their parent entries stay, and
   such a file in the snapshot is left out. Changes = added, modified and deleted files;
4. checks in this order: paths of added and modified files (`ops/rules.json#/path`, `path_invalid`); at most 500 changed
   files, each changed file at most 10 MiB, together at most 50 MiB (`size_exceeded`); the deterministic secret patterns
   in every added or modified file (`secret_detected`; files that do not change are not scanned - a secret that is
   already in the parent is not added by this commit); a snapshot without any change is refused with `no_changes`
   (KOR-10: an empty tree change would reach GitHub, which answers `422`);
5. the export is deleted and only the blobs to upload are kept (`snapshot.trim`); plan and scan run in a worker
   thread, so a large repository never blocks the daemon;
6. dedupe fields `parent_sha` and `tree_sha` (the local root tree SHA); the executor's data: parent, parent tree, tree
   SHA, the tree entries and the number of changed files.

Commit identity (KOR-47, ADR-078 sections 4 and 5; checked first, before the repository, `commit_identity`): the
pre-check runs after the resolution of the acting identity (`identity.resolved`). `committer` (an identity ID, default
the acting identity) and `author` (default the committer) must each be usable by the acting user (`identity.usable`;
`identity_forbidden`, also when the hub refuses the record with `403`); a record that is gone, refused or does not open
with a valid `login` and `user_id` is `identity_unavailable`, and so is a token GitHub names another account for
(`GET /user`, `identity.check_account`). `co_authors` (user IDs; duplicates are refused by the formal checks) must
each be a human who instructed the task (the author of a delivered prompt stored here, `credentials.acting.
instructors`; else `identity_forbidden`) with an own primary identity that is no copy of the operator's account
(`people.account`, login and account ID only; else `identity_unavailable`). `signing` is `key` by default when the
committer identity holds a signing key, else `none`; `key` without one is `identity_unavailable`. The data keeps author
and committer as `{name: login, email: <id>+<login>@users.noreply.github.com}`, the co-authors likewise, the signing
choice and the committer identity (`signer`, to open its key at execution); never a token or key.

Executor: `POST /repos/{r}/git/blobs` (base64) for every added or modified file - each blob read from the snapshot and
checked against its SHA, each answer's SHA compared with the local one; `POST /repos/{r}/git/trees` with `base_tree` =
the parent tree, deletions (`sha: null`) first, then the files; GitHub's tree SHA must equal the local one
(`tree_mismatch`, not retryable); `POST /repos/{r}/git/commits {message, tree, parents: [parent], author, committer,
signature?}`. Author and committer are always sent (explicit attribution, also for the acting identity) with one
runner time (UTC, whole seconds; `signing.Person`). The co-authors follow the message as `Co-authored-by: <login>
<email>` trailer lines after one blank line (a message that ends with a line feed gets one more, one that does not
gets two) and the trailers end with a line feed (`full_message`). With `signing:"key"` the committer identity's seed
signs exactly the commit object GitHub stores (`signing.commit_text`) and the armoured SSH signature is sent as
`signature`; a key that is gone or not valid then is `identity_unavailable`. Result
`{commit_sha, parent_sha, tree_sha, files_changed}`. The scratch directory is removed when the operation ends, kept
after a retryable failure for the next attempt; the snapshot never leaves the device except as these blobs.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from agent_runner.credentials import acting, identities
from agent_runner.credentials.cache import RecordRefused
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError
from agent_runner.github import access, checkout, gitobj, identity, ops, people, signing, snapshot, tarsafe
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.guestlink.errors import GuestError
from agent_runner.operations.extensions import (
    CheckFailed,
    OperationContext,
    OperationFailed,
    PrecheckRequest,
    PrecheckResult,
)
from agent_runner.operations.rules import COMMIT_LIMITS, find_secret, path_valid
from agent_runner.orgs.book import OrgBook
from agent_runner.orgs.db import session as store_session
from agent_runner.sync.http import HubRefusal

MAX_CHANGES = COMMIT_LIMITS["files"]
MAX_FILE = COMMIT_LIMITS["file_bytes"]
MAX_TOTAL = COMMIT_LIMITS["total_bytes"]
SNAPSHOT_MISSING = "The workspace snapshot of the commit is missing."
UNAVAILABLE = "A GitHub identity of the commit is not connected on this device."
NOT_INSTRUCTING = "A co-author must be a human who instructed this task."
NO_COAUTHOR = "A co-author has no own GitHub identity that can be named."
NO_KEY = "The committer identity holds no signing key."


@dataclass(frozen=True)
class Plan:
    tree_sha: str
    entries: list[dict[str, Any]]
    """Tree entries of `POST /git/trees`: deletions (`sha` None) first, then added and modified files."""
    files_changed: int


def _in_scope(path: str, scope: Sequence[str] | None) -> bool:
    return scope is None or any(path == item or path.startswith(item + "/") for item in scope)


def _kind(mode: str) -> str:
    return "commit" if mode == gitobj.SUBMODULE_MODE else "blob"


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:depth]) for depth in range(1, len(parts))]


def plan(base: Mapping[str, tuple[str, str]], snap: snapshot.Snapshot, scope: Sequence[str] | None) -> Plan:
    """The new tree and its entries relative to `base` (see the module docstring); `CheckFailed` on a refusal.
    Linear in the number of files (each path is compared with its ancestors only)."""
    files = {path: found for path, found in snap.files.items() if not tarsafe.protected(path)}
    present = set(files)
    skipped = {item["path"] for item in snap.skipped}
    new: dict[str, tuple[str, str]] = {}
    for path, entry in base.items():
        lineage = [path, *_ancestors(path)]
        kept = tarsafe.protected(path) or any(item in skipped for item in lineage)
        if entry[0] in gitobj.REGULAR_MODES and _in_scope(path, scope) and path not in present and not kept:
            continue  # deleted in the workspace
        if any(item in present for item in lineage[1:]):
            continue  # below a path that is a file now
        new[path] = entry
    for path in present:
        for ancestor in _ancestors(path):
            new.pop(ancestor, None)
    for path, found in files.items():
        old = base.get(path)
        mode = old[0] if old is not None and old[0] in gitobj.REGULAR_MODES else gitobj.FILE_MODE
        new[path] = (mode, found.blob)
    deleted = sorted(path for path in base if path not in new)
    changed = sorted(path for path, entry in new.items() if base.get(path) != entry)
    if any(not path_valid(path) for path in changed):
        raise CheckFailed("path_invalid", message="A changed file has a path that cannot be committed.")
    if len(deleted) + len(changed) > MAX_CHANGES:
        raise CheckFailed("size_exceeded", message="The commit changes more than 500 files.")
    sizes = [snap.files[path].size for path in changed]
    if any(size > MAX_FILE for size in sizes):
        raise CheckFailed("size_exceeded", message="A changed file is larger than 10 MiB.")
    if sum(sizes) > MAX_TOTAL:
        raise CheckFailed("size_exceeded", message="The changed files are larger than 50 MiB together.")
    entries: list[dict[str, Any]] = [
        {"path": path, "mode": base[path][0], "type": _kind(base[path][0]), "sha": None} for path in deleted
    ]
    entries += [{"path": path, "mode": new[path][0], "type": "blob", "sha": new[path][1]} for path in changed]
    return Plan(gitobj.build_tree(new), entries, len(deleted) + len(changed))


def scan(scratch: Any, entries: Sequence[Mapping[str, Any]]) -> None:
    """The secret patterns in every added or modified file of the snapshot."""
    for entry in entries:
        if entry["sha"] is None:
            continue
        text = snapshot.read_blob(scratch, entry["sha"]).decode("latin-1")
        found = find_secret(text)
        if found is not None:
            raise CheckFailed("secret_detected", message=f"A secret ({found}) was found in a changed file.")


async def _parent(github: GitHubClient, request: PrecheckRequest, repo: str, branch: str) -> str:
    try:
        answer = await github.request("GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}")
    except GitHubError as error:
        if error.kind != "not_found":
            raise
    else:
        return ops.text(ops.obj(ops.obj(answer.data).get("object")).get("sha"), ops.SHA_RE)
    async with store_session(request.context.pool) as conn:
        found = await checkout.base_sha(conn, request.session.id, repo)
    if found is None:
        raise CheckFailed("not_found", message="The branch does not exist and the repository was not checked out.")
    return found


async def _parent_files(github: GitHubClient, repo: str, parent: str) -> tuple[str, dict[str, tuple[str, str]]]:
    answer = await github.request("GET", f"/repos/{repo}/git/commits/{parent}")
    tree = ops.text(ops.obj(ops.obj(answer.data).get("tree")).get("sha"), ops.SHA_RE)
    answer = await github.request("GET", f"/repos/{repo}/git/trees/{tree}", params={"recursive": "1"})
    listing = ops.obj(answer.data)
    if listing.get("truncated") is True:
        raise CheckFailed("size_exceeded", message="The parent tree is too large to compare.")
    items = listing.get("tree")
    try:
        return tree, gitobj.flatten(items if isinstance(items, list) else [None])
    except ValueError:
        raise ops.invalid() from None


async def _snapshot(request: PrecheckRequest, repo: str, scratch: Any) -> snapshot.Snapshot:
    paths = request.params.get("paths")
    try:
        guest = await snapshot.guest_for(snapshot.env_of(request.context), request.session)
    except snapshot.NoGuest:
        raise CheckFailed("not_found", message="The session has no VM on this device.") from None
    try:
        async with guest:
            return await snapshot.take(guest, request.session.id, checkout.directory(repo), paths, scratch)
    except GuestError as error:
        code, _ = snapshot.guest_code(error)
        raise CheckFailed(code, message="The workspace export failed.") from None
    except snapshot.TooLarge:
        raise CheckFailed("size_exceeded", message="The workspace export is too large.") from None
    except snapshot.SnapshotInvalid:
        raise CheckFailed("upstream_error", message="The workspace export was not valid.") from None


def _unavailable(message: str = UNAVAILABLE) -> CheckFailed:
    return CheckFailed("identity_unavailable", message=message)


async def _person(
    request: PrecheckRequest, book: OrgBook, identity_id: object, resolved: identity.Resolved
) -> dict[str, Any]:
    """`{name, email, signing, data}` of the identity `identity_id` (None: the acting identity)."""
    data, version = resolved.data(), resolved.version
    if identity_id is not None:
        try:
            found = await identities.meta(request.context, str(identity_id))
        except HubRefusal as refusal:
            raise (identity.forbidden() if refusal.status == 403 else _unavailable()) from None
        except RecordRefused:
            raise _unavailable() from None
        if found is None:
            raise _unavailable()
        user, org = resolved.acting_user, request.session.org_id
        if identity.usable(request.context, book, found.owner, found.owner_kind, user=user, org_id=org) is None:
            raise identity.forbidden()
        data = {
            **data,
            "github_identity": found.id,
            "identity_owner": found.owner,
            "identity_kind": found.owner_kind,
            "identity_primary": False,
        }
        version = found.version
    try:
        async with ops.identity_body(request.context, request.operation, data) as body:
            named, key = people.login_of(body), isinstance(body.get("signing_key"), dict)
    except ops.NotConnected:
        raise _unavailable() from None
    if named is None:
        raise _unavailable()
    # the account behind the token (the acting identity was checked by the resolution, remembered per version)
    await identity.check_account(request.context, request.operation, data, version)
    return {"name": named[1], "email": signing.noreply(*named), "signing": key, "data": data}


async def commit_identity(request: PrecheckRequest) -> dict[str, Any]:
    """Author, committer, co-authors and signing of the commit (module docstring); `CheckFailed` otherwise."""
    params, resolved = request.params, identity.current()
    book = await identity.loaded(request.context)
    committer = await _person(request, book, params.get("committer"), resolved)
    author = committer if "author" not in params else await _person(request, book, params["author"], resolved)
    async with store_session(request.context.pool) as conn:
        humans = await acting.instructors(conn, request.session.task_id)
    co_authors = []
    for human in params.get("co_authors", []):
        if human not in humans:
            raise CheckFailed("identity_forbidden", message=NOT_INSTRUCTING)
        found = await people.account(request.context, human)
        if found is None:
            raise _unavailable(NO_COAUTHOR)
        co_authors.append({"name": found[1], "email": signing.noreply(*found)})
    choice = params.get("signing", "key" if committer["signing"] else "none")
    if choice == "key" and not committer["signing"]:
        raise _unavailable(NO_KEY)
    return {
        "author": {"name": author["name"], "email": author["email"]},
        "committer": {"name": committer["name"], "email": committer["email"]},
        "co_authors": co_authors,
        "signing": choice,
        "signer": committer["data"],
    }


async def check(request: PrecheckRequest) -> PrecheckResult:
    repo, branch = str(request.params["repo"]), str(request.params["branch"])
    context, op_id = request.context, request.operation.id
    attribution = await commit_identity(request)
    async with access.client(request) as github:
        await access.repository(github, repo, write=True)
        parent = await _parent(github, request, repo, branch)
        parent_tree, base = await _parent_files(github, repo, parent)
    snapshot.sweep(context)
    scratch = snapshot.fresh(context, op_id)
    try:
        snap = await _snapshot(request, repo, scratch)
        found = await asyncio.to_thread(plan, base, snap, request.params.get("paths"))
        if found.files_changed == 0:
            below = " below the listed paths" if request.params.get("paths") else ""
            message = f"The workspace directory {checkout.directory(repo)} has no changes{below} against the branch."
            raise CheckFailed("no_changes", message=message)
        await asyncio.to_thread(scan, scratch, found.entries)
        snapshot.trim(scratch, {entry["sha"] for entry in found.entries})
    except BaseException:
        snapshot.remove(context, op_id)
        raise
    data = {
        "parent_sha": parent,
        "parent_tree": parent_tree,
        "tree_sha": found.tree_sha,
        "entries": found.entries,
        "files_changed": found.files_changed,
        "commit": attribution,
    }
    return PrecheckResult(dedupe={"parent_sha": parent, "tree_sha": found.tree_sha}, data=data)


async def _create(ctx: OperationContext, github: GitHubClient, scratch: Any) -> dict[str, Any]:
    repo, data = str(ctx.params["repo"]), ctx.data
    parent, tree_sha = ops.text(data.get("parent_sha"), ops.SHA_RE), ops.text(data.get("tree_sha"), ops.SHA_RE)
    entries: list[dict[str, Any]] = list(data.get("entries", []))
    for entry in entries:
        if entry["sha"] is None:
            continue
        try:
            content = snapshot.read_blob(scratch, entry["sha"])
        except snapshot.SnapshotInvalid:
            raise OperationFailed("not_found", message=SNAPSHOT_MISSING) from None
        blob = {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
        answer = await github.request("POST", f"/repos/{repo}/git/blobs", body=blob)
        if ops.obj(answer.data).get("sha") != entry["sha"]:
            raise OperationFailed("tree_mismatch", message="GitHub stored a blob with another SHA.")
    body: dict[str, Any] = {"base_tree": data["parent_tree"], "tree": entries}
    answer = await github.request("POST", f"/repos/{repo}/git/trees", body=body)
    if ops.obj(answer.data).get("sha") != tree_sha:
        raise OperationFailed("tree_mismatch", message="GitHub built another tree than the snapshot.")
    body = {"message": str(ctx.params["message"]), "tree": tree_sha, "parents": [parent]}
    body = await _attributed(ctx, github, body, data["commit"])
    answer = await github.request("POST", f"/repos/{repo}/git/commits", body=body)
    commit = ops.text(ops.obj(answer.data).get("sha"), ops.SHA_RE)
    return {"commit_sha": commit, "parent_sha": parent, "tree_sha": tree_sha, "files_changed": data["files_changed"]}


def full_message(message: str, co_authors: Sequence[Mapping[str, str]]) -> str:
    """The message with one `Co-authored-by` trailer per co-author after a blank line (module docstring)."""
    if not co_authors:
        return message
    lines = "".join(f"Co-authored-by: {item['name']} <{item['email']}>\n" for item in co_authors)
    return message + ("\n" if message.endswith("\n") else "\n\n") + lines


async def _seed(ctx: OperationContext, signer: Mapping[str, Any]) -> bytes:
    """The committer identity's signing seed (`identity_unavailable` when it is gone or not valid)."""
    try:
        async with ops.identity_body(ctx.context, ctx.operation, signer) as body:
            key = body.get("signing_key")
            return b64u_decode(key.get("seed") if isinstance(key, dict) else None, signing.SEED_BYTES)
    except (ops.NotConnected, CryptoError):
        raise OperationFailed("identity_unavailable", message=NO_KEY) from None


async def _attributed(
    ctx: OperationContext, github: GitHubClient, body: dict[str, Any], found: Mapping[str, Any]
) -> dict[str, Any]:
    """`body` with author, committer, the co-author trailers and, with `signing:"key"`, the signature."""
    when = int(github.wall())
    author = signing.Person(found["author"]["name"], found["author"]["email"], when)
    committer = signing.Person(found["committer"]["name"], found["committer"]["email"], when)
    message = full_message(body["message"], found["co_authors"])
    out = {**body, "message": message, "author": author.api(), "committer": committer.api()}
    if found["signing"] == "key":
        seed = await _seed(ctx, found["signer"])
        text = signing.commit_text(body["tree"], list(body["parents"]), author, committer, message)
        out["signature"] = signing.sign(seed, text)
    return out


async def commit(ctx: OperationContext, github: GitHubClient) -> dict[str, Any]:
    scratch = snapshot.scratch_dir(ctx.context, ctx.operation.id)
    if not scratch.is_dir():
        raise OperationFailed("not_found", message=SNAPSHOT_MISSING)
    try:
        result = await _create(ctx, github, scratch)
    except (GitHubError, OperationFailed) as error:
        if not error.retryable:
            snapshot.remove(ctx.context, ctx.operation.id)
        raise
    snapshot.remove(ctx.context, ctx.operation.id)
    return result


execute = ops.executor(commit)
