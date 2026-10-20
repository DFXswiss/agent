"""Opt-in approval of initial fork CI runs after the local report gate.

A missing review record does not block this approval. This module authorizes execution, never retries a test or approves a review. Configuration and workflow allowlists come only from the trusted default ref.
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


def _has_newer_active(
    runs: list[Mapping[str, Any]],
    pull: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    path = candidate["path"]
    stamp = (_timestamp(candidate.get("created_at")), candidate["id"])
    return any(
        _on_this_pull(run, pull)
        and run.get("path") == path
        and (_timestamp(run.get("created_at")), run["id"]) > stamp
        and run.get("status") in {"queued", "in_progress"}
        for run in runs
    )


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
    paths = config["workflows"]

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
    inventory = _runs(api, assessment.repo, assessment.head_sha)
    held = [
        run for run in inventory
        if _matches(run, pull, paths) and _pending(run)
        and not _has_newer_active(inventory, pull, run)
    ]
    held.sort(key=lambda run: (_timestamp(run.get("created_at")), run["id"]))
    result = []
    auth_comment = None
    from .pr_lifecycle import _recorded_authorization_run_id, record_workflow_approval
    for candidate in held:
        pull = fresh_pull()
        current = _runs(api, assessment.repo, assessment.head_sha)
        if _has_newer_active(current, pull, candidate):
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
        if not dry_run:
            siblings = _runs(api, assessment.repo, assessment.head_sha, event=None)
            recorded_id = _recorded_authorization_run_id(run, siblings, assessment.pr)
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
