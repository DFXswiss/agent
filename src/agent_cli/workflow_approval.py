"""Opt-in approval of initial fork CI runs after a live A38 pass.

This module authorizes execution, never retries a test or approves a review.
Configuration and workflow allowlists come only from the trusted default ref.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

MAX_RUNS = 1000


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


def _runs(api: Any, repo: str, head: str, *, event: str | None = "pull_request") -> list[Mapping[str, Any]]:
    from .a38_guard import GuardError
    items: list[Mapping[str, Any]] = []
    total = None
    for page in range(1, 11):
        params = dict(head_sha=head, per_page=100, page=page)
        if event is not None:
            params["event"] = event
        query = urlencode(params)
        data = api.get_json(f"/repos/{repo}/actions/runs?{query}")
        if not isinstance(data, Mapping) or type(data.get("total_count")) is not int:
            raise GuardError("workflow run inventory missing total_count")
        count = data["total_count"]
        if count < 0 or count >= MAX_RUNS or (total is not None and count != total):
            raise GuardError("workflow run inventory changed or exceeds bound")
        total = count
        batch = data.get("workflow_runs")
        if not isinstance(batch, list) or any(not isinstance(r, Mapping) for r in batch):
            raise GuardError("workflow run inventory invalid")
        items.extend(batch)
        ids = [r.get("id") for r in items]
        if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
            raise GuardError("workflow run inventory has invalid or duplicate IDs")
        if len(items) == total:
            return items
        if len(items) > total or not batch:
            raise GuardError("workflow run inventory incomplete")
    raise GuardError("workflow run pagination bound exceeded")


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


def _stale_held(
    runs: list[Mapping[str, Any]],
    pull: Mapping[str, Any],
    latest: dict[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Held runs on this head that must not stay `action_required`.

    GitHub's PR banner counts every such run, including superseded copies and
    workflows that are not allowlisted. Cancel those so the banner clears.
    Never cancel the latest allowlisted candidate (that one is approved).
    """
    keep = {run["id"] for run in latest.values() if _pending(run)}
    stale: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for run in runs:
        ident = run.get("id")
        if type(ident) is not int or ident in seen or ident in keep:
            continue
        if not _on_this_pull(run, pull) or not _pending(run):
            continue
        seen.add(ident)
        stale.append(run)
    return stale


def _belongs_to_pull(api: Any, run: Mapping[str, Any], pull: Mapping[str, Any]) -> None:
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
        if (linked.get("number") != pull["number"]
                or _field(linked, "head", "sha") != head["sha"]
                or _field(linked, "base", "sha") != pull["base"]["sha"]):
            raise GuardError("workflow run targets another pull request head or base")
        return
    # GitHub returns an empty associations array for private forks. Prove the
    # fork branch identifies exactly this open PR, and that its head contains
    # the current base (an older-base merge cannot change the measured tree).
    pulls = api.paginate(f"/repos/{repo}/pulls?state=open")
    matches = [p for p in pulls if isinstance(p, Mapping) and p.get("state") == "open"
               and _field(p, "head", "ref") == head["ref"]
               and _field(p, "head", "repo", "full_name") == head["repo"]["full_name"]]
    if len(matches) != 1 or matches[0].get("number") != pull["number"]:
        raise GuardError("fork workflow run cannot be uniquely associated with this pull request")
    match = matches[0]
    if _field(match, "head", "sha") != head["sha"] or _field(match, "base", "sha") != pull["base"]["sha"]:
        raise GuardError("fork pull request changed during workflow approval")
    comparison = api.get_json(f"/repos/{repo}/compare/{pull['base']['sha']}...{head['sha']}")
    if not isinstance(comparison, Mapping) or comparison.get("status") not in {"ahead", "identical"}:
        raise GuardError("fork workflow approval requires the current base to be included in the head")
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
        _report_fingerprint, collect_comments, pick_latest_author_report,
        migration_approval,
    )
    if (not assessment.workflow_approval_enabled or assessment.closed or not assessment.ok
            or assessment.status != "pass" or assessment.mode != "enforce"):
        return []
    snap = fetch_pull(api, assessment.repo, assessment.pr)
    trusted = resolve_trusted_guard_config(api, snap)
    config = (trusted.config or {}).get("workflow_approval")
    if not config or not config["enabled"] or snap.head_repo == snap.repo:
        return []
    paths = config["workflows"]

    def fresh_pull() -> Mapping[str, Any]:
        fresh = assess_pull(api, assessment.repo, assessment.pr, dry_run=True)
        fields = ("head_sha", "base_sha", "base_ref", "head_repo", "config_revision", "config_fingerprint",
                  "report_fingerprint", "approval_fingerprint", "policy_sha")
        if (not fresh.ok or fresh.closed or fresh.status != "pass" or fresh.mode != "enforce"
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
    inventory = _runs(api, assessment.repo, assessment.head_sha)
    candidates = _latest(inventory, pull, paths)
    result = []
    for stale in _stale_held(inventory, pull, candidates):
        pull = fresh_pull()
        run = api.get_json(f"/repos/{assessment.repo}/actions/runs/{stale['id']}")
        if not isinstance(run, Mapping) or run.get("id") != stale["id"] or not _on_this_pull(run, pull):
            raise GuardError("workflow run identity changed before cancel")
        if not _pending(run):
            continue
        _belongs_to_pull(api, run, pull)
        final = fetch_pull(api, assessment.repo, assessment.pr)
        config_now = resolve_trusted_guard_config(api, final)
        comments = collect_comments(api, assessment.repo, assessment.pr)
        if (final != snap or config_now.config_revision != trusted.config_revision
                or config_now.fingerprint != trusted.fingerprint
                or _report_fingerprint(pick_latest_author_report(comments, final.author_id)) != assessment.report_fingerprint
                or migration_approval(api, final) != assessment.approval_fingerprint):
            raise GuardError("pull or author evidence changed before workflow cancel")
        run = api.get_json(f"/repos/{assessment.repo}/actions/runs/{stale['id']}")
        if not isinstance(run, Mapping) or run.get("id") != stale["id"] or not _pending(run):
            continue
        if not dry_run:
            status, _, _ = api.request(
                "POST", f"/repos/{assessment.repo}/actions/runs/{run['id']}/cancel", retry=False
            )
            # 202 = cancelled; 409 = already gone from the hold queue.
            if status not in {202, 409}:
                raise GuardError(
                    f"workflow cancel HTTP {status}; Actions write permission is required"
                )
            assessment.writes.append(f"workflow:cancel:{run['id']}")
        result.append(
            {
                "run_id": stale["id"],
                "workflow": stale.get("path"),
                "head": assessment.head_sha,
                "status": "planned-cancel" if dry_run else "cancelled",
            }
        )
    for path, candidate in sorted(candidates.items()):
        if not _pending(candidate):
            continue
        pull = fresh_pull()
        # Re-list all states: a newer queued/passed/failed run supersedes an old
        # blocked run, so never wake that old run and repeat a completed suite.
        latest = _latest(_runs(api, assessment.repo, assessment.head_sha), pull, paths).get(path)
        if latest is None or latest["id"] != candidate["id"] or not _pending(latest):
            continue
        run = api.get_json(f"/repos/{assessment.repo}/actions/runs/{candidate['id']}")
        if not isinstance(run, Mapping) or run.get("id") != candidate["id"] or not _matches(run, pull, paths):
            raise GuardError("workflow run identity changed before approval")
        if not _pending(run):
            continue
        _belongs_to_pull(api, run, pull)
        # Last live checks immediately before the irreversible authorization.
        final = fetch_pull(api, assessment.repo, assessment.pr)
        config_now = resolve_trusted_guard_config(api, final)
        comments = collect_comments(api, assessment.repo, assessment.pr)
        if (final != snap or config_now.config_revision != trusted.config_revision
                or config_now.fingerprint != trusted.fingerprint
                or _report_fingerprint(pick_latest_author_report(comments, final.author_id)) != assessment.report_fingerprint
                or migration_approval(api, final) != assessment.approval_fingerprint):
            raise GuardError("pull or author evidence changed before workflow approval")
        if not dry_run:
            status, _, _ = api.request("POST", f"/repos/{assessment.repo}/actions/runs/{run['id']}/approve", retry=False)
            if status != 201:
                raise GuardError(f"workflow approval HTTP {status}; Actions write permission is required")
            assessment.writes.append(f"workflow:approve:{run['id']}")
            if assessment.lifecycle_enabled:
                from .pr_lifecycle import record_workflow_approval
                record_workflow_approval(api, assessment, run)
        result.append({"run_id": run["id"], "workflow": path, "head": assessment.head_sha,
                       "status": "planned" if dry_run else "approved"})
    return result
