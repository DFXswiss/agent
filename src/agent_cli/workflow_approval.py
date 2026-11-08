"""Opt-in approval of initial fork CI runs after the local report gate.

A missing review record does not block this approval. This module authorizes execution, never retries a test or approves a review. Only an initial held run whose head SHA is the current head or the merge commit is approved; a run on an older commit of the pull request is left alone for the held-run drain. The trusted default ref still requires workflow_approval.workflows, but that list does not filter which held pull_request runs are approved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

MAX_RUNS = 1000
PAGE_SIZE = 100
# Approving an older held run can cancel one of these through cancel-in-progress.
_IN_FLIGHT_STATUSES = frozenset({
    "queued", "in_progress", "waiting", "requested", "pending",
})
# Branch listings stay status-scoped. Ten full pages is MAX_RUNS runs with no
# short page, and that still fails the approval closed. action_required is the
# hold; the rest are the in-flight runs that must stay visible on intermediate
# commits.
_BRANCH_LIST_STATUSES = (
    "action_required",
    "queued",
    "in_progress",
    "waiting",
    "requested",
    "pending",
)


def _field(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _timestamp(value: Any) -> datetime:
    from .a38_guard import GuardError
    if not isinstance(value, str):
        raise GuardError("workflow approval timestamp missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuardError("workflow approval timestamp invalid") from exc
    if result.tzinfo is None:
        raise GuardError("workflow approval timestamp must have a timezone")
    return result.astimezone(timezone.utc)


def _list_runs(
    api: Any,
    repo: str,
    params: Mapping[str, Any],
    *,
    allow_truncated: bool = False,
    single_page: bool = False,
) -> list[Mapping[str, Any]]:
    from .a38_guard import GuardError

    rendered_params = ", ".join(
        f"{key}={str(value).replace(chr(10), ' ')[:80]}"
        for key, value in sorted(params.items())
    )[:500]

    def fail(message: str, total: int | None, rows: int) -> GuardError:
        observed = total if type(total) is int else "unknown"
        return GuardError(
            f"{message} (params: {rendered_params}; "
            f"total_count={observed}; rows={rows})"
        )

    def once() -> tuple[list[Mapping[str, Any]], str | None, int | None]:
        items: list[Mapping[str, Any]] = []
        seen_ids: set[int] = set()
        total_count: int | None = None
        pages = range(1, 2) if single_page else range(1, MAX_RUNS // PAGE_SIZE + 1)
        for page in pages:
            query = urlencode({**params, "per_page": PAGE_SIZE, "page": page})
            data = api.get_json(f"/repos/{repo}/actions/runs?{query}")
            if not isinstance(data, Mapping) or type(data.get("total_count")) is not int:
                raise fail(
                    "workflow run inventory missing total_count", None, len(items)
                )
            # total_count is a response-shape check. It does not end the list.
            if data["total_count"] < 0:
                raise fail(
                    "workflow run inventory missing total_count",
                    data["total_count"],
                    len(items),
                )
            if total_count is None:
                total_count = data["total_count"]
            elif data["total_count"] != total_count:
                return items, "count", data["total_count"]
            batch = data.get("workflow_runs")
            if not isinstance(batch, list) or any(not isinstance(r, Mapping) for r in batch):
                raise fail("workflow run inventory invalid", total_count, len(items))
            if not batch:
                if page == 1 and total_count > 0:
                    raise fail(
                        "workflow run inventory incomplete", total_count, len(items)
                    )
                return items, None, total_count
            batch_ids = [r.get("id") for r in batch]
            if any(type(ident) is not int or ident <= 0 for ident in batch_ids):
                raise fail(
                    "workflow run inventory has invalid or duplicate IDs",
                    total_count,
                    len(items),
                )
            if (
                len(set(batch_ids)) != len(batch_ids)
                or any(ident in seen_ids for ident in batch_ids)
            ):
                return items, "duplicate", total_count
            items.extend(batch)
            seen_ids.update(batch_ids)
            if single_page:
                if len(batch) == PAGE_SIZE:
                    raise fail(
                        "workflow run in-flight listing exceeds one page",
                        total_count,
                        len(items),
                    )
                return items, None, total_count
            if len(batch) < PAGE_SIZE:
                return items, None, total_count
        if allow_truncated:
            # The drain accepts this page-bound partial list; protection is
            # refreshed before each delete.
            return items, None, total_count
        raise fail(
            "workflow run pagination bound exceeded", total_count, len(items)
        )

    listed, shift_cause, observed_total = once()
    if shift_cause is not None:
        # A changed count and a duplicate id are the detectable signs that the
        # paged inventory shifted. A departing row replaced by an entering row
        # is not detectable here. The in-flight listings that guard against
        # cancellation enforce their one-page property with single_page; an
        # undetectable shift in the remaining paged listings can only defer an
        # action_required approval.
        listed, shift_cause, observed_total = once()
    if shift_cause is not None:
        if allow_truncated:
            # A persistent shift may return the last attempt's prefix.
            return listed
        if shift_cause == "count":
            raise fail(
                "workflow run inventory changed or exceeds bound",
                observed_total,
                len(listed),
            )
        raise fail(
            "workflow run inventory has invalid or duplicate IDs",
            observed_total,
            len(listed),
        )
    return listed


def _runs(api: Any, repo: str, head: str, *, event: str | None = "pull_request") -> list[Mapping[str, Any]]:
    params: dict[str, Any] = {"head_sha": head}
    if event is not None:
        params["event"] = event
    return _list_runs(api, repo, params)


def _merge_listed_runs(*groups: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    merged: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for group in groups:
        for run in group:
            ident = run["id"]
            if ident in seen:
                continue
            seen.add(ident)
            merged.append(run)
    return merged


def _merge_sha(pull: Mapping[str, Any]) -> str | None:
    value = pull.get("merge_commit_sha")
    if isinstance(value, str) and value != "":
        return value
    return None


def _commit_shas(api: Any, repo: str, number: int) -> set[str]:
    from .a38_guard import GuardError, fetch_pull_commits
    shas: set[str] = set()
    for item in fetch_pull_commits(api, repo, number):
        sha = item.get("sha")
        if not isinstance(sha, str) or sha == "":
            raise GuardError("pull commit sha missing")
        shas.add(sha)
    return shas


def _pull_request_inventory(api: Any, repo: str, pull: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    head = pull["head"]
    groups = [_runs(api, repo, head["sha"])]
    for status in _BRANCH_LIST_STATUSES:
        groups.append(_list_runs(api, repo, {
            "branch": head["ref"],
            "event": "pull_request",
            "status": status,
        }, single_page=status in _IN_FLIGHT_STATUSES))
    merged = _merge_listed_runs(*groups)
    merge = _merge_sha(pull)
    if merge is not None and not any(run.get("head_sha") == merge for run in merged):
        groups.append(_runs(api, repo, merge))
        merged = _merge_listed_runs(*groups)
    return merged


def _on_this_pull(run: Mapping[str, Any], pull: Mapping[str, Any]) -> bool:
    head, base = pull["head"], pull["base"]
    return (
        run.get("event") == "pull_request"
        and run.get("head_sha") == head["sha"]
        and run.get("head_branch") == head["ref"]
        and isinstance(run.get("repository"), Mapping)
        and run["repository"].get("full_name") == base["repo"]["full_name"]
        and isinstance(run.get("head_repository"), Mapping)
        and run["head_repository"].get("full_name") == head["repo"]["full_name"]
    )


def _matches(run: Mapping[str, Any], pull: Mapping[str, Any], paths: list[str]) -> bool:
    return _on_this_pull(run, pull) and run.get("path") in paths


def _latest(runs: list[Mapping[str, Any]], pull: Mapping[str, Any], paths: list[str]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for run in runs:
        if not _matches(run, pull, paths):
            continue
        path = run["path"]
        key = (_timestamp(run.get("created_at")), run["id"])
        previous = latest.get(path)
        if previous is None or key > (_timestamp(previous.get("created_at")), previous["id"]):
            latest[path] = run
    return latest


def _pending(run: Mapping[str, Any]) -> bool:
    # A later attempt is never auto-approved: this feature does not retry tests.
    return (run.get("status") == "completed" and run.get("conclusion") == "action_required"
            and type(run.get("run_attempt")) is int and run["run_attempt"] == 1)


def _sha_on_pull(run: Mapping[str, Any], pull: Mapping[str, Any], commit_shas: set[str]) -> bool:
    sha = run.get("head_sha")
    if not isinstance(sha, str) or sha == "":
        return False
    if sha == pull["head"]["sha"]:
        return True
    merge = _merge_sha(pull)
    if merge is not None and sha == merge:
        return True
    return sha in commit_shas


def _fork_pull_run(run: Mapping[str, Any], pull: Mapping[str, Any]) -> bool:
    head = pull["head"]
    base = pull["base"]
    return (
        run.get("event") == "pull_request"
        and isinstance(run.get("repository"), Mapping)
        and run["repository"].get("full_name") == base["repo"]["full_name"]
        and run.get("head_branch") == head["ref"]
        and isinstance(run.get("head_repository"), Mapping)
        and run["head_repository"].get("full_name") == head["repo"]["full_name"]
    )


def _release_shape(run: Mapping[str, Any], pull: Mapping[str, Any], commit_shas: set[str]) -> bool:
    return _fork_pull_run(run, pull) and _sha_on_pull(run, pull, commit_shas) and isinstance(run.get("path"), str)


def _in_flight_on_this_pull(
    run: Mapping[str, Any], pull: Mapping[str, Any], commit_shas: set[str],
) -> bool:
    head = pull["head"]
    if run.get("event") != "pull_request":
        return False
    if not isinstance(run.get("head_repository"), Mapping):
        return False
    if run["head_repository"].get("full_name") != head["repo"]["full_name"]:
        return False
    return _sha_on_pull(run, pull, commit_shas)


def _has_newer_active(
    runs: list[Mapping[str, Any]],
    pull: Mapping[str, Any],
    candidate: Mapping[str, Any],
    commit_shas: set[str],
) -> bool:
    path = candidate.get("path")
    stamp = (_timestamp(candidate.get("created_at")), candidate["id"])
    return any(
        _in_flight_on_this_pull(run, pull, commit_shas)
        and run.get("path") == path
        and run.get("status") in _IN_FLIGHT_STATUSES
        and (_timestamp(run.get("created_at")), run["id"]) > stamp
        for run in runs
    )


def _historical_pull_sha(
    run: Mapping[str, Any], pull: Mapping[str, Any], pull_shas: set[str] | None,
) -> bool:
    sha = run.get("head_sha")
    current = pull["head"]["sha"]
    if not isinstance(sha, str) or sha == "" or sha == current:
        return False
    merge = _merge_sha(pull)
    if merge is not None and sha == merge:
        return True
    return pull_shas is not None and sha in pull_shas


def _counts_for_ready_inventory(
    run: Mapping[str, Any], pull: Mapping[str, Any] | None, head_sha: str
) -> bool:
    """A merge-SHA run from another repository is not this pull request.

    The current head SHA is already unique to this pull request. The merge
    SHA can also belong to a base-repository run created when a different
    pull request merged. A merge-commit run that does not name a head
    repository fails closed.
    """
    from .a38_guard import GuardError
    if run.get("head_sha") == head_sha:
        return True
    if not isinstance(pull, Mapping):
        return False
    repository = run.get("head_repository")
    if not isinstance(repository, Mapping):
        raise GuardError("workflow run head repository missing")
    name = repository.get("full_name")
    if not isinstance(name, str) or name == "":
        raise GuardError("workflow run head repository missing")
    head = pull.get("head")
    repo = head.get("repo") if isinstance(head, Mapping) else None
    expected = repo.get("full_name") if isinstance(repo, Mapping) else None
    if not isinstance(expected, str) or expected == "":
        return False
    return name == expected


def _records_ci_auth(run: Mapping[str, Any], pull: Mapping[str, Any]) -> bool:
    sha = run.get("head_sha")
    if sha == pull["head"]["sha"]:
        return True
    merge = _merge_sha(pull)
    return merge is not None and sha == merge


def _foreign_or_ambiguous_pull_link(
    run: Mapping[str, Any], pull: Mapping[str, Any], pull_shas: set[str] | None,
) -> bool:
    """A linked run for another pull request is not approved.

    Skipping it leaves the other held runs of this pull request to be
    approved. An empty association list stays with the private-fork proof.
    """
    links = run.get("pull_requests")
    if not isinstance(links, list) or not links:
        return False
    if len(links) != 1 or not isinstance(links[0], Mapping):
        return True
    linked = links[0]
    head = pull["head"]
    same_number = linked.get("number") == pull["number"]
    same_head = _field(linked, "head", "sha") == head["sha"]
    same_base = _field(linked, "base", "sha") == pull["base"]["sha"]
    if same_number and same_head and same_base:
        return False
    if (
        same_number
        and _historical_pull_sha(run, pull, pull_shas)
        and run.get("head_branch") == head["ref"]
        and isinstance(run.get("head_repository"), Mapping)
        and run["head_repository"].get("full_name") == head["repo"]["full_name"]
    ):
        return False
    return True


def _belongs_to_pull(
    api: Any, run: Mapping[str, Any], pull: Mapping[str, Any], *, pull_shas: set[str] | None = None,
) -> None:
    from .a38_guard import GuardError
    repo = pull["base"]["repo"]["full_name"]
    head = pull["head"]
    links = run.get("pull_requests")
    if not isinstance(links, list):
        raise GuardError("workflow run pull request associations missing")
    if _timestamp(run.get("created_at")) < _timestamp(pull.get("created_at")):
        raise GuardError("workflow run predates this pull request")
    if links:
        if len(links) != 1 or not isinstance(links[0], Mapping):
            raise GuardError("workflow run pull request association is ambiguous")
        linked = links[0]
        same_number = linked.get("number") == pull["number"]
        same_head = _field(linked, "head", "sha") == head["sha"]
        same_base = _field(linked, "base", "sha") == pull["base"]["sha"]
        if same_number and same_head and same_base:
            return
        # Older commit or merge commit of this PR: a different linked head is not
        # another PR when branch and fork match. Foreign and ambiguous links still fail.
        if (
            same_number
            and _historical_pull_sha(run, pull, pull_shas)
            and run.get("head_branch") == head["ref"]
            and isinstance(run.get("head_repository"), Mapping)
            and run["head_repository"].get("full_name") == head["repo"]["full_name"]
        ):
            return
        raise GuardError("workflow run targets another pull request head or base")
    # GitHub returns an empty associations array for private forks. Prove the
    # fork branch identifies exactly this open PR. The approved run is bound to
    # the head commit; a newer base does not change that commit.
    pulls = api.paginate(f"/repos/{repo}/pulls?state=open")
    matches = [p for p in pulls if isinstance(p, Mapping) and p.get("state") == "open"
               and _field(p, "head", "ref") == head["ref"]
               and _field(p, "head", "repo", "full_name") == head["repo"]["full_name"]]
    if len(matches) != 1 or matches[0].get("number") != pull["number"]:
        raise GuardError("fork workflow run cannot be uniquely associated with this pull request")
    match = matches[0]
    if _field(match, "head", "sha") != head["sha"] or _field(match, "base", "sha") != pull["base"]["sha"]:
        raise GuardError("fork pull request changed during workflow approval")
    events = api.paginate(f"/repos/{repo}/issues/{pull['number']}/events")
    for event in events:
        if not isinstance(event, Mapping):
            raise GuardError("pull request event inventory invalid")
        if event.get("event") in {"base_ref_changed", "base_ref_force_pushed", "reopened"}:
            if _timestamp(event.get("created_at")) > _timestamp(run.get("created_at")):
                raise GuardError("workflow run predates a pull request target or lifecycle change")


def approve_workflow_runs(api: Any, assessment: Any, *, dry_run: bool = False) -> list[dict[str, Any]]:
    from .a38_guard import (
        GuardError, assess_pull, fetch_pull, resolve_trusted_guard_config,
        _report_fingerprint, collect_comments, reload_gate_report,
        migration_approval, resolve_write_ready,
    )
    if (
        not assessment.workflow_approval_enabled
        or assessment.closed
        or assessment.mode != "enforce"
        or not assessment.report_gate_ok
    ):
        return []
    snap = fetch_pull(api, assessment.repo, assessment.pr)
    trusted = resolve_trusted_guard_config(api, snap)
    config = (trusted.config or {}).get("workflow_approval")
    if not config or not config["enabled"] or snap.head_repo == snap.repo:
        return []

    def fresh_pull() -> Mapping[str, Any]:
        fresh = assess_pull(
            api,
            assessment.repo,
            assessment.pr,
            dry_run=True,
            event_actor=assessment.event_actor,
        )
        fields = ("head_sha", "base_sha", "base_ref", "head_repo", "config_revision", "config_fingerprint",
                  "report_fingerprint", "approval_fingerprint", "policy_sha")
        if (not fresh.report_gate_ok or fresh.closed or fresh.mode != "enforce"
                or any(getattr(fresh, f) != getattr(assessment, f) for f in fields)):
            raise GuardError("A38 evidence or trusted configuration changed before workflow approval")
        pull = api.get_json(f"/repos/{assessment.repo}/pulls/{assessment.pr}")
        if (not isinstance(pull, Mapping) or pull.get("state") != "open"
                or _field(pull, "head", "sha") != assessment.head_sha
                or _field(pull, "base", "sha") != assessment.base_sha
                or _field(pull, "base", "ref") != assessment.base_ref
                or _field(pull, "head", "repo", "full_name") != assessment.head_repo
                or not isinstance(_field(pull, "head", "ref"), str)):
            raise GuardError("pull request changed before workflow approval")
        return pull

    pull = fresh_pull()
    commit_shas = _commit_shas(api, assessment.repo, assessment.pr)
    inventory = _pull_request_inventory(api, assessment.repo, pull)
    held = [
        run for run in inventory
        if _release_shape(run, pull, commit_shas) and _pending(run)
        and _records_ci_auth(run, pull)
        and not _has_newer_active(inventory, pull, run, commit_shas)
    ]
    held.sort(key=lambda run: (_timestamp(run.get("created_at")), run["id"]))
    result = []
    auth_comment = None
    from .pr_lifecycle import _recorded_authorization_run_id, record_workflow_approval
    for candidate in held:
        pull = fresh_pull()
        commit_shas = _commit_shas(api, assessment.repo, assessment.pr)
        current = _pull_request_inventory(api, assessment.repo, pull)
        if _has_newer_active(current, pull, candidate, commit_shas):
            continue
        run = api.get_json(f"/repos/{assessment.repo}/actions/runs/{candidate['id']}")
        if (
            not isinstance(run, Mapping)
            or run.get("id") != candidate["id"]
            or not _release_shape(run, pull, commit_shas)
        ):
            raise GuardError("workflow run identity changed before approval")
        # Approval releases only the current head and the merge commit. A run
        # on a superseded commit stays held for the drain.
        if not _records_ci_auth(run, pull):
            continue
        if not _pending(run):
            continue
        if _foreign_or_ambiguous_pull_link(run, pull, commit_shas):
            continue
        _belongs_to_pull(api, run, pull, pull_shas=commit_shas)
        # Last live checks immediately before the irreversible authorization.
        final = fetch_pull(api, assessment.repo, assessment.pr)
        config_now = resolve_trusted_guard_config(api, final)
        comments = collect_comments(api, assessment.repo, assessment.pr)
        if (final != snap or config_now.config_revision != trusted.config_revision
                or config_now.fingerprint != trusted.fingerprint
                or _report_fingerprint(reload_gate_report(api, final, comments)) != assessment.report_fingerprint
                or migration_approval(api, final) != assessment.approval_fingerprint):
            raise GuardError("pull or author evidence changed before workflow approval")
        still_ready, _ = resolve_write_ready(
            api, final, event_actor=assessment.event_actor
        )
        from .a38_guard import _docs_report_waiver
        from .readme_only import pull_is_guard_docs_only
        docs_waiver = _docs_report_waiver(
            getattr(assessment, "write_ready_reason", "")
        ) and pull_is_guard_docs_only(api, final.repo, final.number)
        if assessment.write_ready and not still_ready and not docs_waiver:
            raise GuardError("write-ready waiver changed before workflow approval")
        latest = api.get_json(f"/repos/{assessment.repo}/pulls/{assessment.pr}")
        if (
            not isinstance(latest, Mapping)
            or _field(latest, "state") != "open"
            or _field(latest, "head", "sha") != _field(pull, "head", "sha")
            or _field(latest, "base", "sha") != _field(pull, "base", "sha")
            or _field(latest, "base", "ref") != _field(pull, "base", "ref")
            or _field(latest, "head", "repo", "full_name")
            != _field(pull, "head", "repo", "full_name")
            or _field(latest, "merge_commit_sha")
            != _field(pull, "merge_commit_sha")
            or not _records_ci_auth(run, latest)
        ):
            raise GuardError("pull request changed before workflow approval")
        if not dry_run:
            allowed_shas = {assessment.head_sha}
            groups = [_runs(api, assessment.repo, assessment.head_sha, event=None)]
            merge = _merge_sha(pull)
            if merge is not None and merge != assessment.head_sha:
                allowed_shas.add(merge)
                groups.append(_runs(api, assessment.repo, merge, event=None))
            from .pr_lifecycle import _unstarted_non_allowlisted_hold
            lifecycle = {}
            trusted_config = getattr(config_now, "config", None)
            if isinstance(trusted_config, Mapping):
                raw_lifecycle = trusted_config.get("lifecycle")
                if isinstance(raw_lifecycle, Mapping):
                    lifecycle = raw_lifecycle
            required_paths = set(lifecycle.get("required_workflows") or [])
            label_names = {
                _field(label, "name")
                for label in pull.get("labels", [])
                if isinstance(label, Mapping)
            }
            for condition in lifecycle.get("conditional_workflows") or []:
                if not isinstance(condition, Mapping):
                    continue
                bases = condition.get("base_branches") or []
                labels_any = condition.get("labels_any") or []
                workflow = condition.get("workflow")
                if isinstance(workflow, str) and (
                    assessment.base_ref in bases or label_names.intersection(labels_any)
                ):
                    required_paths.add(workflow)
            allowlist = frozenset(config["workflows"])
            siblings = []
            for item in _merge_listed_runs(*groups):
                if not _counts_for_ready_inventory(item, pull, assessment.head_sha):
                    continue
                if _unstarted_non_allowlisted_hold(
                    api, assessment, item,
                    required=required_paths, approval_allowlist=allowlist,
                ):
                    continue
                siblings.append(item)
            recorded_id = _recorded_authorization_run_id(
                run, siblings, assessment.pr, allowed_shas=allowed_shas
            )
            status, _, _ = api.request("POST", f"/repos/{assessment.repo}/actions/runs/{run['id']}/approve", retry=False)
            if status != 201:
                if status != 403:
                    raise GuardError(
                        f"workflow approval HTTP {status}; Actions write permission is required"
                    )
                # A concurrent guard run may have released this run already. GitHub
                # answers 403 for an approve call on a run that no longer waits. Decide
                # that from the run's own state, for any attempt: _pending excludes a
                # later attempt by design, which would misread one that is still waiting.
                current = api.get_json(
                    f"/repos/{assessment.repo}/actions/runs/{run['id']}"
                )
                if not isinstance(current, Mapping) or current.get("id") != run["id"]:
                    raise GuardError(
                        "workflow run identity changed before approval"
                    )
                if (
                    current.get("status") == "completed"
                    and current.get("conclusion") == "action_required"
                ):
                    raise GuardError(
                        f"workflow approval HTTP {status}; Actions write permission is required"
                    )
                # Nothing was authorised here, so record the outcome without claiming an
                # authorisation this guard did not perform.
                assessment.writes.append(f"workflow:approve:{run['id']}:already-released")
                result.append({"run_id": run["id"], "workflow": run["path"],
                               "head": assessment.head_sha, "status": "already-released"})
                continue
            assessment.writes.append(f"workflow:approve:{run['id']}")
            auth_comment = record_workflow_approval(
                api, assessment, run, create=auth_comment is None, existing=auth_comment,
                recorded_run_id=recorded_id,
            )
        result.append({"run_id": run["id"], "workflow": run["path"], "head": assessment.head_sha,
                       "status": "planned" if dry_run else "approved"})
    return result
