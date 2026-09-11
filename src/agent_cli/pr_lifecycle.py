"""Configured PR readiness, based on live CI rather than a cached green rollup.

This reconciler never runs tests, submits reviews, or merges pull requests.
The caller must serialize all guard invocations for the repository.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from .readme_only import pull_is_markdown_only, pull_is_readme_only
from .workflow_approval import _field, _runs, _timestamp

AUTH_MARKER = "<!-- PR-GUARD:CI-AUTH:v1 -->"
STATE_MARKER = "<!-- PR-GUARD:LIFECYCLE:v1 -->"


def a38_passed_job_names(api: Any, assessment: Any, pull: Mapping | None) -> frozenset[str]:
    """Names and ids of jobs that passed in the verified author A38 report.

    Used so a skipped or neutral GitHub required check does not block Ready
    when the matching local-CI job already passed. Cancelled and failed
    GitHub required checks still block.
    """
    if not assessment.ok or assessment.status != "pass" or assessment.report_status != "pass":
        return frozenset()
    from .a38_guard import collect_comments, pick_latest_author_report
    from .local_ci import LocalCiError, parse_comment
    author_id = _field(pull or {}, "user", "id")
    if not isinstance(author_id, int):
        return frozenset()
    comment = pick_latest_author_report(
        collect_comments(api, assessment.repo, assessment.pr), author_id
    )
    if comment is None:
        return frozenset()
    try:
        report = parse_comment(str(comment.get("body") or ""))
    except LocalCiError:
        return frozenset()
    if report.head != assessment.head_sha:
        return frozenset()
    names: set[str] = set()
    for run in report.runs:
        if run.result == "pass":
            names.add(run.name)
            names.add(run.id)
    return frozenset(names)


def a38_covers_required_check(passed_names: frozenset[str], required: str) -> bool:
    """True when a passing A38 job name or id matches the required GitHub check."""
    if not required:
        return False
    for name in passed_names:
        if name == required or required_check_matches(name, required):
            return True
    return False


def required_check_matches(check_name: object, required: str) -> bool:
    """Return whether a GitHub check run satisfies a required_checks entry.

    Reusable-workflow jobs publish ``{caller name} / {called name}``. A required
    name ``Full-stack E2E`` must match that expanded form, not only the exact
    caller name. Other prefixes (``Testing`` vs ``Test``) must not match.
    """
    if not isinstance(check_name, str) or not required:
        return False
    return check_name == required or check_name.startswith(required + " / ")


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


def _last_parenthetical_suffix(reason: str) -> str | None:
    if reason.endswith(")") and " (" in reason:
        return reason.rsplit(" (", 1)[1][:-1]
    return None


def _join_en_phrases(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _join_de_phrases(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} und {parts[1]}"
    return ", ".join(parts[:-1]) + f" und {parts[-1]}"


def visible_transition_sentences(
    record: Mapping, write_ready_reason: str = ""
) -> tuple[str, str]:
    """Return the visible EN and DE sentences for an applied lifecycle comment.

    Names only the blockers present in record['reasons']. Never uses 'or'/'oder'
    to join CI and merge conflicts. One sentence per language.
    """
    raw = record.get("reasons")
    reasons = list(raw) if isinstance(raw, list) else []
    draft = record.get("state") == "draft"
    if not draft and not reasons:
        return (
            "The authorized CI runs are green and no merge conflicts exist; "
            "this pull request is ready for review.",
            "Die freigegebenen CI-Läufe sind grün und es gibt keine Merge-Konflikte; "
            "dieser Pull Request ist bereit zum Review.",
        )

    conflicts = "Merge conflicts" in reasons
    ci_reasons = [r for r in reasons if r != "Merge conflicts"]

    missing = any(
        isinstance(r, str) and r.startswith("Missing required CI:") for r in ci_reasons
    )
    statuses: set[str] = set()
    non_a38_statuses: set[str] = set()
    a38 = False
    for reason in ci_reasons:
        if not isinstance(reason, str):
            continue
        if "A38" in reason:
            a38 = True
        suffix = _last_parenthetical_suffix(reason)
        if suffix is not None:
            statuses.add(suffix)
            if "A38" not in reason:
                non_a38_statuses.add(suffix)

    en_parts: list[str] = []
    de_parts: list[str] = []
    if conflicts:
        en_parts.append("merge conflicts exist")
        de_parts.append("Merge-Konflikte bestehen")

    ci_en: list[str] = []
    ci_de: list[str] = []
    if missing:
        ci_en.append("required CI is missing")
        ci_de.append("erforderliche CI fehlt")
    if "action_required" in statuses:
        ci_en.append("CI is waiting for approval")
        ci_de.append("die CI auf Freigabe wartet")
    if statuses & {"queued", "waiting", "in_progress", "pending"}:
        ci_en.append("CI is still running")
        ci_de.append("die CI noch läuft")
    if non_a38_statuses & {"failure", "failed", "cancelled", "timed_out", "error"}:
        ci_en.append("CI failed")
        ci_de.append("die CI fehlgeschlagen ist")
    if a38:
        ci_en.append("A38 is not green")
        ci_de.append("A38 nicht grün ist")
    if ci_reasons and not ci_en:
        ci_en.append("CI is not green")
        ci_de.append("die CI nicht grün ist")

    en_parts.extend(ci_en)
    de_parts.extend(ci_de)

    if not en_parts:
        en_clause = "the readiness conditions are no longer met"
        de_clause = "die Voraussetzungen für Ready nicht mehr erfüllt sind"
    else:
        en_clause = _join_en_phrases(en_parts)
        de_clause = _join_de_phrases(de_parts)

    if draft:
        return (
            f"This pull request is back in Draft because {en_clause}.",
            f"Dieser Pull Request steht wieder auf Draft, weil {de_clause}.",
        )
    if write_ready_reason == "author has write":
        return (
            f"The author has write; this pull request is ready for review "
            f"even though {en_clause}.",
            f"Der Autor hat Write; dieser Pull Request ist bereit zum Review, "
            f"auch wenn {de_clause}.",
        )
    return (
        f"A write collaborator marked Ready; this pull request is ready for review "
        f"even though {en_clause}.",
        f"Ein Write-Collaborator hat Ready gesetzt; dieser Pull Request ist bereit "
        f"zum Review, auch wenn {de_clause}.",
    )


def _complete_transition_comment(api: Any, assessment: Any, record: dict) -> None:
    en, de = visible_transition_sentences(
        record, write_ready_reason=getattr(assessment, "write_ready_reason", "") or ""
    )
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
    # Skipped/neutral required checks are accepted when the PR file inventory
    # is independently README-only or markdown-only, or a verified author A38
    # report on this head passed a matching job. Cancelled, failed, and
    # missing checks still block.
    accept_skipped_required = pull_is_readme_only(
        api, assessment.repo, assessment.pr
    ) or pull_is_markdown_only(api, assessment.repo, assessment.pr)
    accepted_required = (
        {"success", "skipped", "neutral"}
        if accept_skipped_required
        else {"success"}
    )
    passed_a38 = a38_passed_job_names(api, assessment, pull)
    for path in sorted(required):
        suite = _field(latest.get(path), "check_suite_id")
        for name in config.get("required_checks", {}).get(path, []):
            matches = [c for c in checks if suite is not None and _field(c, "check_suite", "id") == suite
                       and required_check_matches(c.get("name"), name)]
            latest_by_name: dict[str, Mapping] = {}
            for candidate in matches:
                check_name = candidate.get("name")
                if not isinstance(check_name, str):
                    continue
                previous = latest_by_name.get(check_name)
                if previous is None or candidate.get("id", 0) > previous.get("id", 0):
                    latest_by_name[check_name] = candidate
            accepted = (
                {"success", "skipped", "neutral"}
                if a38_covers_required_check(passed_a38, name)
                else accepted_required
            )
            if not latest_by_name or any(
                check.get("status") != "completed" or check.get("conclusion") not in accepted
                for check in latest_by_name.values()
            ):
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


class LifecycleDraftUnchanged(Exception):
    """GraphQL accepted convertToDraft but isDraft is still false."""


def _transition(api: Any, node: str, draft: bool) -> Mapping:
    from .a38_guard import GuardError
    operation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
    query = ("mutation($id: ID!) { " + operation
             + "(input: {pullRequestId: $id}) { pullRequest { id isDraft headRefOid baseRefOid } } }")
    status, data, _ = api.request("POST", "/graphql", body={"query": query, "variables": {"id": node}}, retry=False)
    pull = _field(data, "data", operation, "pullRequest")
    errors = _field(data, "errors")
    detail = ""
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        detail = f": {errors[0].get('message') or 'graphql error'}"
    if status != 200 or errors or _field(pull, "id") != node:
        raise GuardError(f"PR lifecycle mutation failed (HTTP {status}){detail}")
    got = _field(pull, "isDraft")
    if got is not draft:
        if draft and got is False:
            raise LifecycleDraftUnchanged
        if not draft and got is True:
            raise GuardError(
                f"PR lifecycle mutation failed (HTTP {status}): isDraft unchanged; "
                "GITHUB_TOKEN needs contents: write for markPullRequestReadyForReview"
            )
        raise GuardError(f"PR lifecycle mutation failed (HTTP {status}): isDraft={got!r}")
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
    we_drafted = (
        previous.get("state") == "draft"
        and previous.get("head") == snap.head_sha
        and previous.get("base") == snap.base_sha
        and previous.get("phase") == "applied"
    )
    write_hold = getattr(assessment, "write_ready_reason", "") in {
        "author has write",
        "ready by write collaborator",
    }
    restore_write_ready = bool(pull["draft"] and write_hold and we_drafted)
    # Write collaborator Ready hold: do not auto-draft while author or the latest
    # ready_for_review actor has write/maintain/admin on the target repository.
    # Markdown-only is a report waiver only — it does not hold Ready through red CI.
    if not pull["draft"] and reasons and write_hold:
        hold = {
            "repo": assessment.repo,
            "pr": assessment.pr,
            "head": snap.head_sha,
            "base": snap.base_sha,
            "state": "ready",
            "reasons": reasons,
            "phase": "applied",
        }
        if not dry_run and (
            previous.get("phase") != "applied"
            or previous.get("state") != "ready"
            or previous.get("head") != snap.head_sha
            or previous.get("base") != snap.base_sha
        ):
            _save_record(
                api,
                assessment,
                STATE_MARKER,
                hold,
                "A write collaborator holds Ready; this pull request stays ready for review.",
                "Ein Write-Collaborator hält Ready; dieser Pull Request bleibt bereit zum Review.",
            )
        return {"action": "unchanged", "reasons": reasons, "dry_run": dry_run}
    if not pull["draft"] and reasons:
        target = "draft"
    elif pull["draft"] and restore_write_ready:
        # Recover from a lagged timeline or a draft that undid an explicit
        # write-collaborator Ready click, even when CI is still red.
        target = "ready"
    elif pull["draft"] and not reasons and pull.get("mergeable") is True and config["auto_ready"]:
        _, authorization = _own_record(api, assessment, AUTH_MARKER)
        identity = {"repo": assessment.repo, "pr": assessment.pr, "head": snap.head_sha, "base": snap.base_sha}
        owned = authorization.get("runs", [])
        if not isinstance(owned, list):
            owned = []
        # Ignored and otherwise absent workflows are not in `latest`. A stale
        # AUTH row for them must not block Ready after they were dropped from
        # the live inventory.
        current = [
            r for r in owned
            if isinstance(r, Mapping) and r.get("workflow") in latest
        ]
        if (all(authorization.get(k) == v for k, v in identity.items()) and current
                and all(_field(latest.get(r.get("workflow")), "id") == r.get("run_id")
                        for r in current)):
            fresh = assess_pull(
                api, assessment.repo, assessment.pr, dry_run=True,
                event_actor=assessment.event_actor,
            )
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
    if (
        target == "ready"
        and not restore_write_ready
        and (final_reasons or final_pull.get("mergeable") is not True)
    ):
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
        fresh = assess_pull(
            api, assessment.repo, assessment.pr, dry_run=True,
            event_actor=assessment.event_actor,
        )
        fields = ("head_sha", "base_sha", "config_revision", "config_fingerprint", "report_fingerprint", "approval_fingerprint")
        if (not fresh.ok or fresh.status != "pass" or fresh.mode != "enforce"
                or any(getattr(fresh, f) != getattr(assessment, f) for f in fields)):
            raise GuardError("A38 evidence changed before Ready transition")
        if restore_write_ready and not fresh.write_ready:
            raise GuardError("write-ready waiver changed before Ready transition")
    try:
        changed = _transition(api, pull["node_id"], target == "draft")
    except LifecycleDraftUnchanged:
        record["phase"] = "applied"
        record["state"] = "ready"
        _save_record(
            api,
            assessment,
            STATE_MARKER,
            record,
            "Readiness is unchanged; convert to Draft did not take effect.",
            "Der Status bleibt unverändert; die Umstellung auf Draft hat nicht gegriffen.",
        )
        return {"action": "unchanged", "reasons": final_reasons, "dry_run": False}
    assessment.writes.append(f"pull:{target}")
    if target == "ready" and (changed.get("headRefOid") != snap.head_sha or changed.get("baseRefOid") != snap.base_sha):
        try:
            _transition(api, pull["node_id"], True)
        except LifecycleDraftUnchanged as exc:
            raise GuardError("pull changed during Ready transition; Draft restore did not take effect") from exc
        raise GuardError("pull changed during Ready transition; restored Draft")
    confirmed = api.get_json(path)
    want_draft = target == "draft"
    observed = confirmed.get("draft")
    if type(observed) is not bool or observed != want_draft:
        raise GuardError(
            f"PR lifecycle REST draft did not match intended Ready/Draft state (observed {observed!r})"
        )
    _complete_transition_comment(api, assessment, record)
    result["reasons"] = final_reasons
    return result
