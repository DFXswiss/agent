"""Opt-in approval of pending GitHub environment deployments after A38 passes.

This module approves only configured deployment environments. It does not
approve held fork workflow runs, retry tests, dispatch workflows, review pull
requests, or merge them.
"""
from __future__ import annotations

from typing import Any, Mapping

from .workflow_approval import (
    _belongs_to_pull,
    _field,
    _latest,
    _matches,
    _runs,
)


def _pending_environment_ids(
    api: Any, repo: str, run_id: int, environment: str
) -> list[int]:
    """Return matching pending environment IDs, rejecting malformed payloads."""
    from .a38_guard import GuardError

    data = api.get_json(
        f"/repos/{repo}/actions/runs/{run_id}/pending_deployments"
    )
    if not isinstance(data, list):
        raise GuardError("pending deployment inventory is not an array")
    matching: list[int] = []
    seen: set[int] = set()
    for item in data:
        if not isinstance(item, Mapping):
            raise GuardError("pending deployment inventory contains a non-object item")
        deployed_environment = item.get("environment")
        if not isinstance(deployed_environment, Mapping):
            raise GuardError("pending deployment environment missing")
        ident = deployed_environment.get("id")
        name = deployed_environment.get("name")
        if type(ident) is not int or ident <= 0 or not isinstance(name, str) or not name:
            raise GuardError("pending deployment environment id or name invalid")
        if ident in seen:
            raise GuardError("pending deployment inventory has duplicate environment IDs")
        seen.add(ident)
        if name == environment:
            matching.append(ident)
    return matching


def approve_environment_deployments(
    api: Any, assessment: Any, *, dry_run: bool = False
) -> list[dict[str, Any]]:
    """Approve allowlisted pending deployments after a fresh enforce pass."""
    from .a38_guard import (
        GuardError,
        _report_fingerprint,
        assess_pull,
        collect_comments,
        fetch_pull,
        migration_approval,
        pick_latest_author_report,
        resolve_trusted_guard_config,
        resolve_write_ready,
    )

    if (
        not assessment.environment_approval_enabled
        or assessment.closed
        or not assessment.ok
        or assessment.status != "pass"
        or assessment.mode != "enforce"
    ):
        return []

    snap = fetch_pull(api, assessment.repo, assessment.pr)
    trusted = resolve_trusted_guard_config(api, snap)
    config = (trusted.config or {}).get("environment_approval")
    if not config or not config["enabled"]:
        return []
    environment = config["environment"]
    paths = config["workflows"]

    def fresh_pull() -> Mapping[str, Any]:
        fresh = assess_pull(
            api,
            assessment.repo,
            assessment.pr,
            dry_run=True,
            event_actor=assessment.event_actor,
        )
        fields = (
            "head_sha",
            "base_sha",
            "base_ref",
            "head_repo",
            "config_revision",
            "config_fingerprint",
            "report_fingerprint",
            "approval_fingerprint",
            "policy_sha",
        )
        if (
            not fresh.ok
            or fresh.closed
            or fresh.status != "pass"
            or fresh.mode != "enforce"
            or any(
                getattr(fresh, field) != getattr(assessment, field)
                for field in fields
            )
        ):
            raise GuardError(
                "A38 evidence or trusted configuration changed before environment approval"
            )
        pull = api.get_json(
            f"/repos/{assessment.repo}/pulls/{assessment.pr}"
        )
        if (
            not isinstance(pull, Mapping)
            or pull.get("state") != "open"
            or _field(pull, "head", "sha") != assessment.head_sha
            or _field(pull, "base", "sha") != assessment.base_sha
            or _field(pull, "base", "ref") != assessment.base_ref
            or _field(pull, "head", "repo", "full_name")
            != assessment.head_repo
            or not isinstance(_field(pull, "head", "ref"), str)
        ):
            raise GuardError("pull request changed before environment approval")
        return pull

    pull = fresh_pull()
    candidates = _latest(
        _runs(api, assessment.repo, assessment.head_sha), pull, paths
    )
    result: list[dict[str, Any]] = []
    auth_comment = None
    from .pr_lifecycle import record_workflow_approval

    for path, candidate in sorted(candidates.items()):
        pending_ids = _pending_environment_ids(
            api, assessment.repo, candidate["id"], environment
        )
        if not pending_ids:
            continue

        pull = fresh_pull()
        latest = _latest(
            _runs(api, assessment.repo, assessment.head_sha), pull, paths
        ).get(path)
        if latest is None or latest["id"] != candidate["id"]:
            continue
        run = api.get_json(
            f"/repos/{assessment.repo}/actions/runs/{candidate['id']}"
        )
        if (
            not isinstance(run, Mapping)
            or run.get("id") != candidate["id"]
            or not _matches(run, pull, paths)
        ):
            raise GuardError("workflow run identity changed before environment approval")
        _belongs_to_pull(api, run, pull)

        final = fetch_pull(api, assessment.repo, assessment.pr)
        config_now = resolve_trusted_guard_config(api, final)
        comments = collect_comments(api, assessment.repo, assessment.pr)
        if (
            final != snap
            or config_now.config_revision != trusted.config_revision
            or config_now.fingerprint != trusted.fingerprint
            or _report_fingerprint(
                pick_latest_author_report(comments, final.author_id)
            )
            != assessment.report_fingerprint
            or migration_approval(api, final) != assessment.approval_fingerprint
        ):
            raise GuardError(
                "pull or author evidence changed before environment approval"
            )
        still_ready, _ = resolve_write_ready(
            api, final, event_actor=assessment.event_actor
        )
        from .a38_guard import _docs_report_waiver
        from .readme_only import pull_is_guard_docs_only

        docs_waiver = _docs_report_waiver(
            getattr(assessment, "write_ready_reason", "")
        ) and pull_is_guard_docs_only(api, final.repo, final.number)
        if assessment.write_ready and not still_ready and not docs_waiver:
            raise GuardError("write-ready waiver changed before environment approval")

        pending_ids = _pending_environment_ids(
            api, assessment.repo, run["id"], environment
        )
        if not pending_ids:
            continue
        if not dry_run:
            status, _, _ = api.request(
                "POST",
                f"/repos/{assessment.repo}/actions/runs/{run['id']}/pending_deployments",
                body={
                    "environment_ids": pending_ids,
                    "state": "approved",
                    "comment": "A38 enforce pass",
                },
                retry=False,
            )
            if status != 200:
                raise GuardError(
                    f"environment approval HTTP {status}; Actions write permission and environment reviewer access are required"
                )
            assessment.writes.append(f"environment:approve:{run['id']}")
            auth_comment = record_workflow_approval(
                api,
                assessment,
                run,
                create=auth_comment is None,
                existing=auth_comment,
            )
        result.append(
            {
                "run_id": run["id"],
                "workflow": path,
                "environment": environment,
                "head": assessment.head_sha,
                "status": "planned" if dry_run else "approved",
            }
        )
    return result
