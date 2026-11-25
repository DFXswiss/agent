"""Reviews, checks and the merge of a pull request (KOR-51, ADR-090 sections 4-6; 03-api.md section 9).

- `github.list_reviews`: `GET /repos/{r}/pulls/{n}` (the head) and `GET …/pulls/{n}/reviews` (GitHub's reviews, the
  newest `REVIEWS_SHOWN`; bodies cut to the contract's length), and the platform verdicts this device knows for the
  pull request (the newest `VERDICTS_SHOWN`; `truncated` when either list had more) within the session's reach (its
  task for reach `task`, its workgroup otherwise; `reviews.store`).
- `github.get_checks`: `GET …/commits/{ref}/check-runs` and `GET …/commits/{ref}/status` of the commit `ref` names:
  every check run `{name, status, conclusion}` and the newest status per context `{context, state}` (at most
  `CHECKS_SHOWN` each, `truncated`) and one `state`: `failure` when a finished run or a status failed (a conclusion
  other than `success`, `neutral` or `skipped`; a status `failure` or `error`), else `pending` while one is not
  finished, else `success`, and `none` without any.
- `github.merge` (pre-check and executor): the pull request is open, no draft and its head is `head_sha`
  (`merge_outdated` otherwise), its base branch is not protected by the workgroup (`branch_protected`), and the
  workgroup's merge criteria and separation of duties hold (`reviews.criteria`, `rights_denied` with the sentence of
  the first criterion not met). The executor reads and checks everything again right before the merge call (a verdict
  `rejected` or a failed check that arrived while the operation waited is not missed), then `PUT …/pulls/{n}/merge`
  with `sha` = `head_sha` and the method (default `merge`): GitHub merges only exactly that head. GitHub's refusals
  are passed on: `405` (not mergeable by GitHub's own rules - protection, required reviews or checks, conflicts) is
  `rights_denied` with GitHub's message, `409` (the head moved) `merge_outdated`.
- `github.push` (`guarded_push`): with any merge rule of the workgroup a push onto the repository's default branch is
  refused (`branch_protected`): the default branch changes only through `github.merge`, so the rules cannot be bypassed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from agent_runner.github import access, ops, push
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations import settings as workgroup_settings
from agent_runner.operations.extensions import (
    CheckFailed,
    OperationContext,
    OperationFailed,
    PrecheckRequest,
    PrecheckResult,
)
from agent_runner.operations.rules import protected
from agent_runner.operations.settings import WorkgroupSettings
from agent_runner.orgs.db import session as store_session
from agent_runner.profiles import check as profile_check
from agent_runner.reviews import criteria, store
from agent_runner.sessions.records import LocalSession

REVIEWS_SHOWN = 100
CHECKS_SHOWN = 1000
VERDICTS_SHOWN = 1000
"""The newest items one answer carries (the contract's bounds); `truncated` says that there were more."""
BODY_LENGTH = 65536
NAME_LENGTH = 1024
GOOD_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
BAD_STATUSES = frozenset({"failure", "error"})
OUTDATED = "The head of the pull request is no longer head_sha; read it again and decide on the new head."
NOT_OPEN = "The pull request is not open, or it is a draft."
REFUSED = (
    "GitHub refused the merge by its own rules (branch protection, required reviews or checks, or a conflict); read the"
    " pull request and its checks."
)
REVIEW_STATE_RE = re.compile(r"^[A-Z_]{1,32}$")
WORD_RE = re.compile(r"^[a-z_]{1,32}$")


def _time(value: Any) -> str:
    return str(value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z")


async def _pull(github: GitHubClient, repo: str, number: int) -> dict[str, Any]:
    answer = await github.request("GET", f"/repos/{repo}/pulls/{number}")
    data = ops.obj(answer.data)
    head, base = ops.obj(data.get("head")), ops.obj(data.get("base"))
    return {
        "open": data.get("state") == "open" and data.get("merged") is not True and data.get("draft") is not True,
        "head_sha": ops.text(head.get("sha"), ops.SHA_RE),
        "base_ref": str(base.get("ref")),
    }


def _review(item: object) -> tuple[dict[str, Any], tuple[str | None, str, str | None]] | None:
    try:
        data = ops.obj(item)
        user = data.get("user")
        login = ops.obj(user).get("login") if user is not None else None
        author = None if login is None else ops.text(login, ops.LOGIN_RE)
        state = ops.text(data.get("state"), REVIEW_STATE_RE)
        commit = None if data.get("commit_id") is None else ops.text(data.get("commit_id"), ops.SHA_RE)
        submitted = None if data.get("submitted_at") is None else ops.text(data.get("submitted_at"), ops.TIME_RE)
        number = ops.integer(data.get("id"))
    except GitHubError:
        return None
    body = str(data.get("body") or "")[:BODY_LENGTH]
    entry = {
        "id": number,
        "author": author,
        "state": state,
        "commit_id": commit,
        "submitted_at": submitted,
        "body": body,
    }
    return entry, (author, state, commit)


async def _reviews(github: GitHubClient, repo: str, number: int) -> list[tuple[dict[str, Any], Any]]:
    items = await github.items(f"/repos/{repo}/pulls/{number}/reviews")
    return [found for found in map(_review, items) if found is not None]


def checks_state(runs: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> str:
    """The one state of a head's checks (module docstring)."""
    if not runs and not statuses:
        return "none"
    failed = any(run["status"] == "completed" and run["conclusion"] not in GOOD_CONCLUSIONS for run in runs) or any(
        status["state"] in BAD_STATUSES for status in statuses
    )
    if failed:
        return "failure"
    waiting = any(run["status"] != "completed" for run in runs) or any(s["state"] == "pending" for s in statuses)
    return "pending" if waiting else "success"


async def _checks(github: GitHubClient, repo: str, ref: str) -> dict[str, Any]:
    runs_answer = await github.request("GET", f"/repos/{repo}/commits/{ref}/check-runs", params={"per_page": 100})
    status_answer = await github.request("GET", f"/repos/{repo}/commits/{ref}/status")
    found_runs = ops.obj(runs_answer.data).get("check_runs")
    combined = ops.obj(status_answer.data)
    found_statuses = combined.get("statuses")
    if not isinstance(found_runs, list) or not isinstance(found_statuses, list):
        raise ops.invalid()
    runs = [
        {
            "name": str(ops.obj(run).get("name"))[:NAME_LENGTH] or "check",
            "status": ops.text(ops.obj(run).get("status"), WORD_RE),
            "conclusion": None
            if ops.obj(run).get("conclusion") is None
            else ops.text(ops.obj(run).get("conclusion"), WORD_RE),
        }
        for run in found_runs
    ]
    statuses = [
        {
            "context": str(ops.obj(item).get("context"))[:NAME_LENGTH] or "default",
            "state": ops.text(ops.obj(item).get("state"), WORD_RE),
        }
        for item in found_statuses
    ]
    truncated = len(runs) > CHECKS_SHOWN or len(statuses) > CHECKS_SHOWN
    return {
        "sha": ops.text(combined.get("sha"), ops.SHA_RE),
        "state": checks_state(runs, statuses),
        "check_runs": runs[:CHECKS_SHOWN],
        "statuses": statuses[:CHECKS_SHOWN],
        "truncated": truncated,
    }


async def list_reviews(ctx: OperationContext, github: GitHubClient) -> dict[str, Any]:
    repo, number = str(ctx.params["repo"]), int(ctx.params["number"])
    pull = await _pull(github, repo, number)
    found = [entry for entry, _key in await _reviews(github, repo, number)]
    reach = await profile_check.reach(ctx.context.pool, ctx.session.id, ctx.session.parent_id)
    async with store_session(ctx.context.pool) as conn:
        verdicts = await store.verdicts(
            conn, ctx.session.wg_id, repo, number, ctx.session.task_id if reach == "task" else None
        )
    return {
        "number": number,
        "head_sha": pull["head_sha"],
        "reviews": found[-REVIEWS_SHOWN:],
        "verdicts": [verdict.as_json(_time(verdict.created_at)) for verdict in verdicts[-VERDICTS_SHOWN:]],
        "truncated": len(found) > REVIEWS_SHOWN or len(verdicts) > VERDICTS_SHOWN,
    }


async def get_checks(ctx: OperationContext, github: GitHubClient) -> dict[str, Any]:
    return await _checks(github, str(ctx.params["repo"]), str(ctx.params["ref"]))


async def _decide(
    github: GitHubClient,
    *,
    pool: Any,
    session: LocalSession,
    settings: WorkgroupSettings,
    params: Any,
    failure: Callable[..., Exception],
) -> str:
    """Every check of the merge (module docstring); the base branch. `failure` is the exception class to raise."""
    repo, number, head = str(params["repo"]), int(params["number"]), str(params["head_sha"]).lower()
    pull = await _pull(github, repo, number)
    if not pull["open"]:
        raise failure("merge_unsupported", message=NOT_OPEN)
    if pull["head_sha"] != head:
        raise failure("merge_outdated", message=OUTDATED)
    if protected(pull["base_ref"], settings.protected_branches):
        raise failure("branch_protected", message="The base branch of the pull request is protected.")
    wanted, separate = settings.merge_criteria, settings.separate_duties
    committers: frozenset[str] = frozenset()
    verdicts: tuple[store.Verdict, ...] = ()
    if separate or wanted.passed_verdicts > 0:
        shas = {
            str(ops.obj(item).get("sha")).lower()
            for item in await github.items(f"/repos/{repo}/pulls/{number}/commits")
        }
        async with store_session(pool) as conn:
            committers = frozenset(await store.committers(conn, session.wg_id, shas))
            verdicts = tuple(await store.verdicts(conn, session.wg_id, repo, number))
    state = (await _checks(github, repo, head))["state"] if wanted.checks_green else None
    approvals: frozenset[str] = frozenset()
    if wanted.github_approvals > 0:
        approvals = criteria.approvals_of((key for _entry, key in await _reviews(github, repo, number)), head)
    facts = criteria.Facts(
        head_sha=head,
        session_id=session.id,
        verdicts=verdicts,
        committers=committers,
        checks_state=state,
        approvals=approvals,
    )
    refused = criteria.refusal(wanted, separate, facts)
    if refused is not None:
        raise failure("rights_denied", message=refused)
    return str(pull["base_ref"])


DIRECT_PUSH = (
    "This workgroup has merge rules: its default branch changes only through github.merge of a pull request. Push to"
    " another branch and open a pull request."
)


async def guarded_push(request: PrecheckRequest) -> PrecheckResult:
    """`github.push`: the push pre-check, and with merge rules of the workgroup no push onto the repository's default
    branch (it would bypass the rules, ADR-090 section 6)."""
    found = await push.check(request)
    settings = request.settings
    if criteria.active(settings.merge_criteria, settings.separate_duties):
        repo = str(request.params["repo"])
        async with access.client(request) as github:
            data = await access.repository(github, repo)
        if request.params["branch"] == data.get("default_branch"):
            raise CheckFailed("branch_protected", message=DIRECT_PUSH)
    return found


async def merge_check(request: PrecheckRequest) -> PrecheckResult:
    async with access.client(request) as github:
        try:
            await _decide(
                github,
                pool=request.context.pool,
                session=request.session,
                settings=request.settings,
                params=request.params,
                failure=CheckFailed,
            )
        except GitHubError as error:
            if error.kind == "not_found":
                raise CheckFailed("not_found", message="The pull request does not exist.") from None
            raise CheckFailed("upstream_error", message="GitHub could not be read for the merge.") from None
    return PrecheckResult()


async def merge(ctx: OperationContext, github: GitHubClient) -> dict[str, Any]:
    async with store_session(ctx.context.pool) as conn:
        current = await workgroup_settings.load(conn, ctx.env.storage_key, ctx.operation.wg_id)
    await _decide(
        github, pool=ctx.context.pool, session=ctx.session, settings=current, params=ctx.params, failure=OperationFailed
    )
    repo, number = str(ctx.params["repo"]), int(ctx.params["number"])
    body = {"sha": str(ctx.params["head_sha"]).lower(), "merge_method": str(ctx.params.get("method", "merge"))}
    try:
        answer = await github.request("PUT", f"/repos/{repo}/pulls/{number}/merge", body=body)
    except GitHubError as error:
        if error.status == 405:
            raise OperationFailed("rights_denied", message=REFUSED) from None
        if error.status == 409:
            raise OperationFailed("merge_outdated", message=OUTDATED) from None
        raise
    data = ops.obj(answer.data)
    if data.get("merged") is not True:
        raise ops.invalid()
    return {"merged": True, "sha": ops.text(data.get("sha"), ops.SHA_RE), "number": number}
