"""Configured PR readiness, based on live CI rather than a cached green rollup.

This reconciler never runs tests, submits reviews, or merges pull requests.
The caller must serialize all guard invocations for the repository.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .readme_only import pull_is_readme_only
from .workflow_approval import _field, _runs, _timestamp

AUTH_MARKER = "<!-- PR-GUARD:CI-AUTH:v1 -->"
STATE_MARKER = "<!-- PR-GUARD:LIFECYCLE:v1 -->"


def _own_record(api: Any, assessment: Any, marker: str) -> tuple[Mapping | None, dict]:
    from .a38_guard import GuardError, collect_comments
    own_id, _ = api.resolve_own_user()
    comments = collect_comments(api, assessment.repo, assessment.pr)
    records = [c for c in comments if _field(c, "user", "id") == own_id
               and str(c.get("body", "")).startswith(marker + "\n")]
    if len(records) > 1 and marker == AUTH_MARKER:
        raise GuardError("ambiguous bot lifecycle audit comments")
    if not records:
        return None, {}
    comment = max(records, key=lambda c: c["id"])
    try:
        data = json.loads(comment["body"].split("```json\n", 1)[1].split("\n```", 1)[0])
    except (ValueError, IndexError, TypeError) as exc:
        raise GuardError("invalid bot lifecycle audit comment") from exc
    if not isinstance(data, dict):
        raise GuardError("invalid bot lifecycle audit record")
    return comment, data


def _save_record(api: Any, assessment: Any, marker: str, record: dict,
                 en: str, de: str, *, create: bool = False) -> None:
    from .a38_guard import GuardError
    existing, _ = _own_record(api, assessment, marker)
    if create:
        existing = None
    body = (f"{marker}\nEN:\n{en}\n\nDE:\n{de}\n\n<details>\n<summary>Details</summary>\n\n"
            + "```json\n" + json.dumps(record, indent=2, sort_keys=True) + "\n```\n\n</details>")
    if existing and existing.get("body") == body:
        return
    path = (f"/repos/{assessment.repo}/issues/comments/{existing['id']}" if existing
            else f"/repos/{assessment.repo}/issues/{assessment.pr}/comments")
    status, _, _ = api.request("PATCH" if existing else "POST", path, body={"body": body}, retry=False)
    if not 200 <= status < 300:
        raise GuardError(f"bot lifecycle audit comment HTTP {status}")
    assessment.writes.append("lifecycle:comment")


def _complete_transition_comment(api: Any, assessment: Any, record: dict) -> None:
    draft = record["state"] == "draft"
    en = ("This pull request is back in Draft because CI is not fully green or merge conflicts exist."
          if draft else "The authorized CI runs are green and no merge conflicts exist; this pull request is ready for review.")
    de = ("Dieser Pull Request steht wieder auf Draft, weil die CI noch nicht vollständig grün ist oder Merge-Konflikte bestehen."
          if draft else "Die freigegebenen CI-Läufe sind grün und es gibt keine Merge-Konflikte; dieser Pull Request ist bereit zum Review.")
    _save_record(api, assessment, STATE_MARKER, {**record, "phase": "applied"}, en, de)


def record_workflow_approval(api: Any, assessment: Any, run: Mapping) -> None:
    """Persist only an authorization whose POST returned 201, before the next one."""
    _, previous = _own_record(api, assessment, AUTH_MARKER)
    identity = {"repo": assessment.repo, "pr": assessment.pr,
                "head": assessment.head_sha, "base": assessment.base_sha}
    runs = previous.get("runs", []) if all(previous.get(k) == v for k, v in identity.items()) else []
    runs = [r for r in runs if r.get("workflow") != run["path"]]
    runs.append({"run_id": run["id"], "workflow": run["path"]})
    _save_record(api, assessment, AUTH_MARKER, {**identity, "runs": runs},
                 "I have authorized the recorded CI runs; their results are still pending.",
                 "Ich habe die dokumentierten CI-Läufe freigegeben; ihre Ergebnisse stehen noch aus.")


def _checks(api: Any, repo: str, head: str) -> list[Mapping]:
    from .a38_guard import GuardError
    result: list[Mapping] = []
    total = None
    for page in range(1, 11):
        data = api.get_json(f"/repos/{repo}/commits/{head}/check-runs?filter=latest&per_page=100&page={page}")
        count = _field(data, "total_count")
        batch = _field(data, "check_runs")
        if (type(count) is not int or not 0 <= count < 1000
                or (total is not None and count != total)
                or not isinstance(batch, list) or any(not isinstance(c, Mapping) for c in batch)):
            raise GuardError("CI check inventory invalid, changed, or exceeds bound")
        total = count
        result.extend(batch)
        ids = [c.get("id") for c in result]
        if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
            raise GuardError("CI check inventory has invalid or duplicate IDs")
        if len(result) == total:
            return result
        if len(result) > total or not batch:
            raise GuardError("CI check inventory incomplete")
    raise GuardError("CI check inventory exceeds page bound")


def ci_state(api: Any, assessment: Any, config: Mapping, pull: Mapping | None = None) -> tuple[list[str], dict[str, Mapping]]:
    """Missing, waiting, running and failed required workflows all block Ready."""
    from .a38_guard import GuardError
    required = set(config["required_workflows"])
    labels = {_field(label, "name") for label in (pull or {}).get("labels", [])}
    for condition in config.get("conditional_workflows", []):
        if assessment.base_ref in condition["base_branches"] or labels.intersection(condition["labels_any"]):
            required.add(condition["workflow"])
    runs = _runs(api, assessment.repo, assessment.head_sha, event=None)
    latest: dict[str, Mapping] = {}
    ignored_suites = set()
    superseded_suites = set()
    for run in runs:
        if run.get("head_sha") != assessment.head_sha:
            raise GuardError("CI workflow inventory contains another head")
        path = run.get("path")
        if not isinstance(path, str):
            raise GuardError("CI workflow inventory lacks a workflow path")
        if path in config["ignored_workflows"]:
            ignored_suites.add(run.get("check_suite_id"))
            continue
        # The head SHA can belong to several PRs; an explicit foreign PR link
        # cannot provide evidence for this one. Empty private-fork links remain
        # usable for observation, but never establish authorization ownership.
        links = run.get("pull_requests")
        if isinstance(links, list) and links and not any(_field(p, "number") == assessment.pr for p in links):
            continue
        previous = latest.get(path)
        if previous is None or (_timestamp(run.get("created_at")), run["id"]) > (_timestamp(previous.get("created_at")), previous["id"]):
            if previous:
                superseded_suites.add(previous.get("check_suite_id"))
            latest[path] = run
        else:
            superseded_suites.add(run.get("check_suite_id"))
    reasons = [f"Missing required CI: {path}" for path in sorted(required) if path not in latest]
    for path, run in sorted(latest.items()):
        accepted = {"success"} if path in required else {"success", "skipped", "neutral"}
        if run.get("status") != "completed" or run.get("conclusion") not in accepted:
            reasons.append(f"CI not green: {path} ({run.get('conclusion') or run.get('status') or 'unknown'})")
    # An old run's check suite must not override the latest workflow result.
    excluded = (ignored_suites | superseded_suites) - {None}
    excluded -= {r.get("check_suite_id") for r in latest.values()}
    checks = _checks(api, assessment.repo, assessment.head_sha)
    # Skipped/neutral required checks are accepted only when the PR file
    # inventory is independently README-only. Missing checks still block.
    readme_only = pull_is_readme_only(api, assessment.repo, assessment.pr)
    accepted_required = (
        {"success", "skipped", "neutral"} if readme_only else {"success"}
    )
    for path in sorted(required):
        suite = _field(latest.get(path), "check_suite_id")
        for name in config.get("required_checks", {}).get(path, []):
            matches = [c for c in checks if suite is not None and _field(c, "check_suite", "id") == suite and c.get("name") == name]
            check = max(matches, key=lambda c: c["id"]) if matches else {}
            if check.get("status") != "completed" or check.get("conclusion") not in accepted_required:
                reasons.append(f"Required CI check not green: {path} / {name}")
    newest: dict[tuple, Mapping] = {}
    for check in checks:
        if _field(check, "check_suite", "id") in excluded:
            continue
        key = (_field(check, "app", "id"), check.get("name"))
        if key not in newest or check["id"] > newest[key]["id"]:
            newest[key] = check
    for check in newest.values():
        # A workflow can intentionally skip individual conditional jobs while
        # succeeding overall. Standalone skipped checks cannot establish green.
        suite = _field(check, "check_suite", "id")
        successful_suite = suite is not None and any(r.get("check_suite_id") == suite
            and r.get("status") == "completed" and (r.get("conclusion") == "success"
                or (path not in required and r.get("conclusion") in {"skipped", "neutral"})) for path, r in latest.items())
        acceptable = check.get("conclusion") == "success" or (successful_suite and check.get("conclusion") in {"skipped", "neutral"})
        if check.get("status") != "completed" or not acceptable:
            reasons.append(f"CI check not green: {check.get('name')} ({check.get('conclusion') or check.get('status') or 'unknown'})")
    statuses = api.paginate(f"/repos/{assessment.repo}/commits/{assessment.head_sha}/statuses")
    seen = set()
    for status in statuses:  # GitHub returns newest first.
        context = _field(status, "context")
        if not isinstance(context, str):
            raise GuardError("CI status inventory invalid")
        if context not in seen and status.get("state") != "success":
            reasons.append(f"CI status not green: {context} ({status.get('state') or 'unknown'})")
        seen.add(context)
    return reasons, latest


def _transition(api: Any, node: str, draft: bool) -> Mapping:
    from .a38_guard import GuardError
    operation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
    query = ("mutation($id: ID!) { " + operation
             + "(input: {pullRequestId: $id}) { pullRequest { id isDraft headRefOid baseRefOid } } }")
    status, data, _ = api.request("POST", "/graphql", body={"query": query, "variables": {"id": node}}, retry=False)
    pull = _field(data, "data", operation, "pullRequest")
    if status != 200 or _field(data, "errors") or _field(pull, "id") != node or _field(pull, "isDraft") is not draft:
        raise GuardError(f"PR lifecycle mutation failed (HTTP {status})")
    return pull


def reconcile_lifecycle(api: Any, assessment: Any, *, dry_run: bool = False) -> dict:
    from .a38_guard import GuardError, assess_pull, fetch_pull, resolve_trusted_guard_config
    if assessment.closed or not assessment.lifecycle_enabled:
        return {}
    if assessment.scope_decision == "exclude":
        return {}
    snap = fetch_pull(api, assessment.repo, assessment.pr)
    trusted = resolve_trusted_guard_config(api, snap)
    config = (trusted.config or {}).get("lifecycle")
    if not config or not config["enabled"]:
        return {}
    if snap.head_sha != assessment.head_sha or snap.base_sha != assessment.base_sha:
        raise GuardError("pull changed before lifecycle assessment")
    path = f"/repos/{assessment.repo}/pulls/{assessment.pr}"
    pull = api.get_json(path)
    if pull.get("state") != "open":
        return {}
    if type(pull.get("draft")) is not bool or not isinstance(pull.get("node_id"), str):
        raise GuardError("pull lifecycle state missing")
    # Recover the explanatory comment if an earlier process died after the
    # mutation. The durable intent precedes it and never claims success early.
    _, previous = _own_record(api, assessment, STATE_MARKER)
    if (previous.get("phase") == "planned" and previous.get("head") == snap.head_sha
            and previous.get("base") == snap.base_sha
            and previous.get("state") == ("draft" if pull["draft"] else "ready") and not dry_run):
        _complete_transition_comment(api, assessment, previous)
    reasons, latest = ci_state(api, assessment, config, pull)
    if pull.get("mergeable") is False:
        reasons.insert(0, "Merge conflicts")
    target = None
    if not pull["draft"] and reasons:
        target = "draft"
    elif pull["draft"] and not reasons and pull.get("mergeable") is True and config["auto_ready"]:
        _, authorization = _own_record(api, assessment, AUTH_MARKER)
        identity = {"repo": assessment.repo, "pr": assessment.pr, "head": snap.head_sha, "base": snap.base_sha}
        owned = authorization.get("runs", [])
        if (all(authorization.get(k) == v for k, v in identity.items()) and owned
                and all(_field(latest.get(r.get("workflow")), "id") == r.get("run_id") for r in owned)):
            fresh = assess_pull(api, assessment.repo, assessment.pr, dry_run=True)
            if fresh.ok and fresh.status == "pass" and fresh.mode == "enforce" and fresh.head_sha == snap.head_sha and fresh.base_sha == snap.base_sha:
                target = "ready"
    result = {"action": target or "unchanged", "reasons": reasons, "dry_run": dry_run}
    if target is None or dry_run:
        return result
    # Re-read state/config and CI immediately before changing readiness.
    final_snap = fetch_pull(api, assessment.repo, assessment.pr)
    final_config = resolve_trusted_guard_config(api, final_snap)
    final_pull = api.get_json(path)
    if (final_snap != snap or final_config.fingerprint != trusted.fingerprint
            or final_config.config_revision != trusted.config_revision
            or final_pull.get("draft") != pull["draft"]):
        raise GuardError("pull or configuration changed before lifecycle transition")
    final_reasons, _ = ci_state(api, assessment, config, final_pull)
    if final_pull.get("mergeable") is False:
        final_reasons.insert(0, "Merge conflicts")
    if target == "ready" and (final_reasons or final_pull.get("mergeable") is not True):
        return {"action": "unchanged", "reasons": final_reasons, "dry_run": False}
    if target == "draft" and not final_reasons:
        return {"action": "unchanged", "reasons": [], "dry_run": False}
    record = {"repo": assessment.repo, "pr": assessment.pr, "head": snap.head_sha,
              "base": snap.base_sha, "state": target, "reasons": final_reasons, "phase": "planned"}
    _save_record(api, assessment, STATE_MARKER, record,
                 "I am checking the final conditions for the documented readiness change.",
                 "Ich prüfe die letzten Voraussetzungen für die dokumentierte Statusänderung.", create=True)
    if fetch_pull(api, assessment.repo, assessment.pr) != snap:
        raise GuardError("pull changed after lifecycle intent; no readiness change")
    if target == "ready":
        fresh = assess_pull(api, assessment.repo, assessment.pr, dry_run=True)
        fields = ("head_sha", "base_sha", "config_revision", "config_fingerprint", "report_fingerprint", "approval_fingerprint")
        if (not fresh.ok or fresh.status != "pass" or fresh.mode != "enforce"
                or any(getattr(fresh, f) != getattr(assessment, f) for f in fields)):
            raise GuardError("A38 evidence changed before Ready transition")
    changed = _transition(api, pull["node_id"], target == "draft")
    assessment.writes.append(f"pull:{target}")
    if target == "ready" and (changed.get("headRefOid") != snap.head_sha or changed.get("baseRefOid") != snap.base_sha):
        _transition(api, pull["node_id"], True)
        raise GuardError("pull changed during Ready transition; restored Draft")
    _complete_transition_comment(api, assessment, record)
    result["reasons"] = final_reasons
    return result
