"""GitHub activities, CI observation, formal approve, Ready, and merge."""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .allow import GATE_PAIRS, evaluate_allow
from .coordinator_common import (
    ACCEPT_BODY,
    ACCEPT_MARKER_PREFIX,
    CI_ACTION_REQUIRED,
    CI_PENDING,
    CI_SUCCESS,
    CoordinatorError,
    QUESTION_MARKER_PREFIX,
    STATUS_MARKER_PREFIX,
    Runner,
    account_for,
    as_int,
    coord,
    coordinator_env,
    gh_json,
    gh_list,
    is_sha,
    redact,
    save_task,
    scoped,
    strip_row,
    target_repo,
    text,
)
from .coordinator_config import WorkerConfig
from .coordinator_exec import run_bounded
from .coordinator_git import (
    execute_github,
    queue_activity,
    repo_cfg,
    verify_signed_clean_head,
)
from .coordinator_lanes import invalidate_head_evidence, latest_gates, set_checklist
from .github_act import ACTIVITY_MARKER
from .github_accounts import AccountError
from .store import Store, StoreError, utcnow

# Fixed JSON contract for configured readiness_argv (trusted operator script).
# Tied to exact HEAD and base. Not model/repo input and not a policy DSL.
READINESS_CONTRACT = (
    '{"head":"<40-hex>","base":"<40-hex-or-configured-base>",'
    '"contributing_ok":true,'
    '"deviation":{"declared":false}'
    "|{\"declared\":true,\"granted\":true,\"granted_by\":\"<reply_login>\","
    '"evidence":"<human grant provenance>"}}'
)


def acceptance_marker(repo: str, number: int, session_id: str) -> str:
    return f"{ACCEPT_MARKER_PREFIX}{repo}#{number}:{session_id} -->"


def post_issue_comment(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    *,
    repo: str,
    number: int,
    body: str,
    kind: str,
) -> str:
    """Publish a deterministic idempotent source-issue status/question comment."""
    safe = redact(body, limit=2000)
    activity_id = str(
        uuid5(
            NAMESPACE_URL,
            f"coordinator-{kind}:{worker.session_id}:{repo}:{number}:{safe[:120]}",
        )
    )
    marker = f"{STATUS_MARKER_PREFIX}{kind}:{activity_id} -->"
    if kind == "question":
        marker = f"{QUESTION_MARKER_PREFIX}{activity_id} -->"
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="comment.post",
        payload={"repo": repo, "number": number, "body": f"{safe}\n{marker}", "target": "issue"},
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if row is None or row.get("execution_status") != "done":
        err = ""
        if row is not None:
            err = redact(str(row.get("execution_error") or row.get("execution_status") or "pending"))
        raise CoordinatorError(f"issue comment {kind} not verified published: {err}")
    return activity_id


def publish_blocker(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    message: str,
    *,
    kind: str = "blocker",
) -> list[str]:
    c = coord(task)
    source = c.get("source") if isinstance(c.get("source"), dict) else None
    lines = [f"blocked: {redact(message)}"]
    if source is None:
        return lines
    try:
        post_issue_comment(
            store,
            worker,
            runner,
            repo=str(source["repo"]),
            number=int(source["number"]),
            body=f"Blocked: {redact(message)}",
            kind=kind,
        )
        lines.append(f"blocker published on {source['repo']}#{source['number']}")
    except (CoordinatorError, StoreError) as exc:
        # These subclass SystemExit — must not be treated as successful publication.
        lines.append(f"blocker publish failed locally visible: {redact(str(exc))}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"blocker publish failed locally visible: {redact(str(exc))}")
    return lines


def verify_issue_assigned(runner: Runner, repo: str, number: int, login: str) -> dict[str, Any]:
    data = gh_json(runner, ["gh", "api", f"repos/{repo}/issues/{number}"])
    if not isinstance(data, dict):
        raise CoordinatorError("issue lookup failed")
    if data.get("pull_request") is not None:
        raise CoordinatorError("target is a pull request, not an issue")
    state = str(data.get("state") or "").lower()
    if state != "open":
        raise CoordinatorError(f"issue {repo}#{number} is {state or 'unknown'}")
    assignees = data.get("assignees")
    if not isinstance(assignees, list):
        raise CoordinatorError("issue assignees missing")
    logins = {
        str(a.get("login")).casefold()
        for a in assignees
        if isinstance(a, dict) and isinstance(a.get("login"), str)
    }
    if login.casefold() not in logins:
        raise CoordinatorError(f"issue {repo}#{number} is no longer assigned to configured login")
    return data


def phase_accept(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    source = c["source"]
    repo = str(source["repo"])
    number = int(source["number"])
    scoped_runner = scoped(store, worker.session_id, runner)
    account = account_for(store, worker.session_id)
    try:
        verify_issue_assigned(scoped_runner, repo, number, account.login)
    except CoordinatorError as exc:
        c["phase"] = "blocked"
        c["blocker"] = str(exc)
        save_task(store, task)
        return publish_blocker(store, worker, task, runner, str(exc), kind="unassigned")
    events = gh_list(scoped_runner, ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/events"])
    assignments = [event for event in events if isinstance(event, dict)
                   and event.get("event") == "assigned"
                   and str((event.get("assignee") or {}).get("login", "")).casefold() == account.login.casefold()
                   and isinstance(event.get("id"), int) and not isinstance(event.get("id"), bool)]
    latest = max(assignments, key=lambda event: event["id"], default={})
    actor = latest.get("actor") or {}
    if latest.get("id", 0) <= 0 or actor.get("type") != "User" or not text(actor.get("login")):
        raise CoordinatorError("assignment has no verified human mandate")
    source["assignment_actor"] = actor["login"]
    source["assignment_event_id"] = latest["id"]
    source["assignment_at"] = latest.get("created_at")
    marker = acceptance_marker(repo, number, worker.session_id)
    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-accept:{worker.session_id}:{repo}:{number}"))
    body = f"{ACCEPT_BODY}\n{marker}"
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="comment.post",
        payload={"repo": repo, "number": number, "body": body, "target": "issue"},
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if row is None or row.get("execution_status") != "done":
        err = ""
        if row is not None:
            err = redact(str(row.get("execution_error") or "acceptance comment not verified"))
        raise CoordinatorError(f"acceptance comment failed before model start: {err}")
    c["acceptance_activity_id"] = activity_id
    set_checklist(store, task, "session_registered", "ja",
                  f"session {worker.session_id} active", source="script")
    set_checklist(store, task, "spec_written", "ja",
                  f"GitHub assignment event {latest['id']} by {actor['login']} on {repo}#{number}",
                  source="human")
    c["phase"] = "checkout"
    save_task(store, task)
    return [f"acceptance published {repo}#{number}"]


def _rollup_state(check: dict[str, Any]) -> str:
    for key in ("conclusion", "state", "status"):
        raw = check.get(key)
        if isinstance(raw, str) and raw:
            return raw.lower()
    return ""


def _paginate_workflow_runs(runner: Runner, repo: str, head: str) -> list[dict[str, Any]]:
    """Paginate Actions runs for an exact head; fail closed on truncation/unknown shape."""
    owner, name = repo.split("/", 1)
    page = 1
    runs: list[dict[str, Any]] = []
    while page <= 20:
        raw = gh_json(
            runner,
            [
                "gh",
                "api",
                f"repos/{owner}/{name}/actions/runs?head_sha={head}&per_page=100&page={page}",
            ],
        )
        if not isinstance(raw, dict):
            raise CoordinatorError("workflow inventory has unexpected shape")
        batch = raw.get("workflow_runs")
        if not isinstance(batch, list):
            raise CoordinatorError("workflow inventory missing workflow_runs")
        for item in batch:
            if isinstance(item, dict):
                runs.append(item)
        total = as_int(raw.get("total_count"))
        if total is not None and len(runs) >= total:
            break
        if len(batch) < 100:
            break
        page += 1
    else:
        raise CoordinatorError("workflow inventory pagination truncated")
    return runs


def _latest_run_attempts(runs: list[dict[str, Any]], head: str) -> dict[str, dict[str, Any]]:
    """Disambiguate by workflow path + event; keep highest attempt/id per key."""
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        if str(run.get("head_sha") or "").lower() != head.lower():
            continue
        path = run.get("path")
        event = run.get("event") or ""
        if not isinstance(path, str) or not path:
            continue
        key = f"{path}|{event}"
        prev = latest.get(key)
        run_attempt = as_int(run.get("run_attempt")) or 0
        run_id = as_int(run.get("id")) or 0
        if prev is None:
            latest[key] = run
            continue
        prev_attempt = as_int(prev.get("run_attempt")) or 0
        prev_id = as_int(prev.get("id")) or 0
        if run_attempt > prev_attempt or (run_attempt == prev_attempt and run_id > prev_id):
            latest[key] = run
    return latest


def fetch_failure_logs(
    runner: Runner,
    repo: str,
    latest: dict[str, dict[str, Any]],
    failures: list[str],
) -> str:
    """Fetch plain-text failed-job logs. Never treat ZIP archive bytes as text."""
    chunks: list[str] = []
    inaccessible = False
    failed_runs: list[tuple[str, dict[str, Any]]] = []
    for key, run in latest.items():
        path = str(run.get("path") or key)
        conclusion = str(run.get("conclusion") or "").lower()
        status = str(run.get("status") or "").lower()
        if status == "completed" and conclusion and conclusion not in CI_SUCCESS:
            failed_runs.append((path, run))
            continue
        # Also match rollup failure names when inventory path differs.
        if any(path in f or key in f or path.rsplit("/", 1)[-1] in f for f in failures):
            failed_runs.append((path, run))
    if not failed_runs and failures:
        # Rollup reported failures but inventory had no matching failed run.
        inaccessible = True
        chunks.append("failing workflow logs inaccessible")
    seen_ids: set[int] = set()
    for path, run in failed_runs:
        run_id = as_int(run.get("id"))
        if run_id is None or run_id in seen_ids:
            if run_id is None:
                inaccessible = True
                chunks.append(f"{path}: logs inaccessible")
            continue
        seen_ids.add(run_id)
        attempt = as_int(run.get("run_attempt")) or 1
        argv = [
            "gh",
            "run",
            "view",
            str(run_id),
            "--repo",
            repo,
            "--log-failed",
            "--attempt",
            str(attempt),
        ]
        try:
            completed = runner(argv)
        except OSError:
            inaccessible = True
            chunks.append(f"{path}: logs inaccessible")
            continue
        if completed.returncode != 0:
            inaccessible = True
            chunks.append(f"{path}: logs inaccessible")
            continue
        raw = completed.stdout or ""
        # ZIP / binary archives must not be fed to the implementer as "logs".
        if raw.startswith("PK") or "\x00" in raw[:200]:
            inaccessible = True
            chunks.append(f"{path}: logs inaccessible (archive, not plain text)")
            continue
        chunks.append(redact(raw[:1500]))
    if inaccessible and not any("logs inaccessible" not in c for c in chunks):
        return "failing workflow logs inaccessible"
    if not chunks:
        return "failing workflow logs inaccessible"
    return "\n".join(chunks)


def phase_ci(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    """Exact-head PR check rollup AND head workflow inventory. Fail closed.

    This core observes cumulative GitHub CI targets only. Target-repository
    policy / A38 live join belongs to configured readiness_argv. No generic
    cancelled/skipped bypass. Empty or malformed evidence is not green.
    action_required is an authorization blocker, not a source-code failure.
    """
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    head = str(c.get("head_sha") or "")
    if number is None or not head:
        raise CoordinatorError("CI observation requires PR number and head")
    scoped_runner = scoped(store, worker.session_id, runner)
    pr = gh_json(
        scoped_runner,
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            target,
            "--json",
            "statusCheckRollup,headRefOid,state,isDraft,author,baseRefName,mergeable",
        ],
    )
    if not isinstance(pr, dict):
        raise CoordinatorError("PR view failed")
    if str(pr.get("headRefOid") or "").lower() != head.lower():
        c["head_sha"] = str(pr.get("headRefOid") or head).lower()
        invalidate_head_evidence(c, c["head_sha"])
        c["phase"] = "tests"
        save_task(store, task)
        return ["PR head changed; invalidating evidence"]
    rollup = pr.get("statusCheckRollup")
    if rollup is None:
        return [f"CI pending on {head[:7]} (rollup absent)"]
    if not isinstance(rollup, list):
        raise CoordinatorError("PR check rollup malformed")
    try:
        runs = _paginate_workflow_runs(scoped_runner, target, head)
    except CoordinatorError as exc:
        return publish_blocker(store, worker, task, runner, f"CI inventory: {exc}", kind="ci-inventory")
    latest = _latest_run_attempts(runs, head)

    pending = False
    failures: list[str] = []
    action_required: list[str] = []
    successes = 0

    for check in rollup:
        if not isinstance(check, dict):
            raise CoordinatorError("PR check rollup entry malformed")
        state = _rollup_state(check)
        name = str(check.get("name") or check.get("context") or "check")
        if state in CI_PENDING or state == "":
            pending = True
        elif state in CI_ACTION_REQUIRED:
            action_required.append(name)
        elif state in CI_SUCCESS:
            successes += 1
        else:
            # cancelled/skipped/failure/neutral/pass/passing — not success here.
            failures.append(f"{name}:{state or 'unknown'}")

    for key, run in latest.items():
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        path = str(run.get("path") or key)
        if status != "completed":
            pending = True
            continue
        if conclusion in CI_ACTION_REQUIRED:
            action_required.append(path)
        elif conclusion in CI_SUCCESS:
            successes += 1
        else:
            failures.append(f"{path}:{conclusion or 'unknown'}")

    if action_required:
        c["phase"] = "blocked"
        c["resume_phase"] = "ci"
        c["blocker"] = "CI action_required (external authorization)"
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            "GitHub CI reports action_required (authorization), not a code failure: "
            + ", ".join(action_required[:5]),
            kind="ci-action-required",
        )

    if not rollup and not latest:
        return [f"CI pending on {head[:7]} (no checks yet)"]
    if pending and not failures:
        return [f"CI pending on {head[:7]}"]
    if failures:
        logs = fetch_failure_logs(scoped_runner, target, latest, failures)
        if "inaccessible" in logs and not any(
            line for line in logs.splitlines() if "inaccessible" not in line and line.strip()
        ):
            c["phase"] = "blocked"
            c["resume_phase"] = "ci"
            c["blocker"] = "CI failed but logs inaccessible"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                f"CI failed on {head[:7]} but workflow logs are inaccessible",
                kind="ci-logs",
            )
        c["findings"] = redact(f"CI failed on {head[:7]}:\n" + "\n".join(failures) + "\n" + logs)
        c["phase"] = "implement"
        task["state"] = "implementing"
        evidence = c.setdefault("evidence", {})
        if isinstance(evidence, dict):
            evidence["ci_green"] = False
            evidence["ci_head"] = head
        invalidate_head_evidence(c, head)
        save_task(store, task)
        return [f"CI failed on {head[:7]}; routing to implementer"]
    # Fail closed: require successful evidence in BOTH rollup and inventory.
    rollup_ok = any(
        isinstance(check, dict) and _rollup_state(check) in CI_SUCCESS for check in rollup
    )
    inventory_ok = any(
        str(run.get("status") or "") == "completed" and str(run.get("conclusion") or "") in CI_SUCCESS
        for run in latest.values()
    )
    if not rollup or not latest or not rollup_ok or not inventory_ok or successes <= 0:
        return [f"CI pending on {head[:7]} (incomplete rollup/inventory success evidence)"]
    evidence = c.setdefault("evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
        c["evidence"] = evidence
    evidence["ci_green"] = True
    evidence["ci_head"] = head
    evidence["ci_observed_at"] = utcnow()
    c["phase"] = "readiness"
    save_task(store, task)
    return [f"CI green on {head[:7]}"]


def _fresh_ci_still_green(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    head: str,
) -> None:
    """Re-observe CI on this tick; do not trust a stale ci_green flag."""
    c = coord(task)
    # Temporarily keep phase; call observation logic inline.
    target = target_repo(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    if number is None:
        raise CoordinatorError("CI recheck requires PR number")
    scoped_runner = scoped(store, worker.session_id, runner)
    pr = gh_json(
        scoped_runner,
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            target,
            "--json",
            "statusCheckRollup,headRefOid",
        ],
    )
    if str(pr.get("headRefOid") or "").lower() != head.lower():
        raise CoordinatorError("PR head changed during readiness")
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        raise CoordinatorError("CI rollup missing on recheck")
    runs = _paginate_workflow_runs(scoped_runner, target, head)
    latest = _latest_run_attempts(runs, head)
    if not latest:
        raise CoordinatorError("CI inventory empty on recheck")
    rollup_ok = False
    for check in rollup:
        if not isinstance(check, dict):
            raise CoordinatorError("CI rollup malformed on recheck")
        state = _rollup_state(check)
        if state in CI_PENDING or state == "":
            raise CoordinatorError("CI pending on recheck")
        if state in CI_SUCCESS:
            rollup_ok = True
        elif state not in CI_SUCCESS:
            raise CoordinatorError(f"CI not green on recheck ({state})")
    if not rollup_ok:
        raise CoordinatorError("CI rollup has no successful check on recheck")
    inventory_ok = False
    for run in latest.values():
        if str(run.get("status") or "") != "completed":
            raise CoordinatorError("CI inventory pending on recheck")
        conclusion = str(run.get("conclusion") or "")
        if conclusion in CI_SUCCESS:
            inventory_ok = True
        else:
            raise CoordinatorError("CI inventory not green on recheck")
    if not inventory_ok:
        raise CoordinatorError("CI inventory has no successful run on recheck")


def _parse_readiness_result(stdout: str, *, head: str, base: str, base_name: str) -> dict[str, Any]:
    """Parse trusted readiness_argv JSON. Fail closed on missing/mismatched proof."""
    raw = (stdout or "").strip()
    if not raw:
        raise CoordinatorError(
            "readiness produced no JSON; required contract: " + READINESS_CONTRACT
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoordinatorError(
            "readiness stdout is not JSON; required contract: " + READINESS_CONTRACT
        ) from exc
    if not isinstance(data, dict):
        raise CoordinatorError("readiness JSON must be an object")
    result_head = str(data.get("head") or "").lower()
    if not is_sha(result_head) or result_head != head.lower():
        raise CoordinatorError("readiness JSON head does not match exact clean signed HEAD")
    result_base = str(data.get("base") or "")
    base_ok = is_sha(result_base.lower()) and result_base.lower() == base.lower()
    if not base_ok:
        raise CoordinatorError("readiness JSON base does not match pinned base")
    if data.get("contributing_ok") is not True:
        raise CoordinatorError("readiness JSON contributing_ok is not true")
    deviation = data.get("deviation")
    if not isinstance(deviation, dict):
        raise CoordinatorError("readiness JSON requires deviation object")
    return data


def _apply_readiness_checklist(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    readiness: dict[str, Any],
    *,
    head: str,
) -> None:
    """Close contributing/deviation from trusted readiness before Ready. No inferred grants."""
    set_checklist(
        store,
        task,
        "contributing_ok",
        "ja",
        f"trusted readiness_argv contributing_ok on {head}",
        source="script",
    )
    deviation = readiness["deviation"]
    declared = deviation.get("declared")
    if declared is False:
        set_checklist(
            store,
            task,
            "deviation_declared",
            "n_a",
            (
                "trusted readiness_argv reported no deviation; "
                f"human assignment mandate {coord(task).get('source', {})}"
            ),
            source="human",
        )
        set_checklist(
            store,
            task,
            "deviation_granted",
            "n_a",
            "no deviation declared; not claiming a grant",
            source="human",
        )
        return
    if declared is not True:
        raise CoordinatorError("readiness deviation.declared must be boolean")
    if deviation.get("granted") is not True:
        raise CoordinatorError("declared deviation without granted=true; refusing inferred grant")
    granted_by = str(deviation.get("granted_by") or "").casefold()
    if not granted_by or granted_by not in {x.casefold() for x in worker.reply_logins}:
        raise CoordinatorError("deviation grant must cite an authorized reply_logins login")
    evidence = text(deviation.get("evidence"))
    if evidence is None:
        raise CoordinatorError("deviation grant requires explicit human grant evidence")
    set_checklist(
        store,
        task,
        "deviation_declared",
        "ja",
        f"trusted readiness + human grant by {granted_by}: {redact(evidence)}",
        source="human",
    )
    set_checklist(
        store,
        task,
        "deviation_granted",
        "ja",
        f"granted_by={granted_by}; {redact(evidence)}",
        source="human",
    )


def phase_readiness(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    source = c["source"]
    cfg = repo_cfg(worker, str(source["repo"]))
    worktree = str(c["worktree"])
    head = verify_signed_clean_head(store, worker, runner, worktree)
    if head != str(c.get("head_sha") or "").lower():
        invalidate_head_evidence(c, head)
        c["head_sha"] = head
        c["phase"] = "tests"
        save_task(store, task)
        return ["head changed before readiness"]

    env = coordinator_env(c, worker, cfg)
    completed = run_bounded(
        list(cfg.readiness_argv),
        timeout=worker.check_timeout,
        cwd=worktree,
        env=env,
        clear_ambient_github=True,
    )
    if completed.returncode != 0:
        raise CoordinatorError(
            redact(f"readiness failed: {(completed.stderr or completed.stdout or '')[:500]}")
        )
    base_sha = str(c.get("base_sha") or "")
    readiness = _parse_readiness_result(
        completed.stdout or "",
        head=head,
        base=base_sha,
        base_name=cfg.base,
    )

    # Re-verify clean signed head AFTER readiness (command may have touched files).
    head_after = verify_signed_clean_head(store, worker, runner, worktree)
    if head_after != head:
        invalidate_head_evidence(c, head_after)
        c["head_sha"] = head_after
        c["phase"] = "tests"
        save_task(store, task)
        return ["head modified by readiness; invalidating evidence"]

    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    if number is None:
        raise CoordinatorError("readiness requires PR number")
    scoped_runner = scoped(store, worker.session_id, runner)
    pr = gh_json(
        scoped_runner,
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            target,
            "--json",
            "headRefOid,baseRefName,author,isDraft,state,mergeable",
        ],
    )
    account = account_for(store, worker.session_id)
    if str(pr.get("headRefOid") or "").lower() != head:
        raise CoordinatorError("PR head does not match clean signed head")
    author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
    if str(author.get("login") or "").casefold() != account.login.casefold():
        raise CoordinatorError("PR author does not match configured worker login")
    if str(pr.get("baseRefName") or "") != cfg.base:
        raise CoordinatorError("PR base mismatch")
    if str(pr.get("state") or "").upper() != "OPEN":
        raise CoordinatorError("PR is not open")
    if str(pr.get("mergeable") or "").upper() != "MERGEABLE":
        raise CoordinatorError(f"PR not mergeable ({pr.get('mergeable')})")
    if pr.get("isDraft") is not True:
        raise CoordinatorError("PR must still be draft before formal approve")

    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if not evidence.get("tests_pass") or evidence.get("tests_head") != head:
        raise CoordinatorError("tests not green on current head")
    _fresh_ci_still_green(store, worker, task, runner, head)
    latest = latest_gates(store, task["id"])
    for stage, dimension, vendor in GATE_PAIRS:
        g = latest.get((stage, dimension))
        if g is None or g.get("verdict") != "approved" or g.get("head_sha") != head or g.get("vendor") != vendor:
            raise CoordinatorError(f"missing approved gate {stage}/{dimension} on {head[:7]}")
    # Close policy/deviation from trusted readiness BEFORE Ready — never after merge.
    _apply_readiness_checklist(store, worker, task, readiness, head=head)
    evidence = c.setdefault("evidence", {})
    if isinstance(evidence, dict):
        evidence["readiness_head"] = head
        evidence["readiness_base"] = base_sha or cfg.base
        evidence["readiness_at"] = utcnow()
    c["phase"] = "formal_approve"
    save_task(store, task)
    return [f"readiness ok on {head[:7]}"]


def _discover_formal_approve(
    runner: Runner,
    *,
    repo: str,
    number: int,
    marker: str,
    head: str,
    login: str,
) -> dict[str, Any] | None:
    owner, name = repo.split("/", 1)
    reviews = gh_list(
        runner,
        ["gh", "api", "--paginate", "--slurp", f"repos/{owner}/{name}/pulls/{number}/reviews"],
    )
    for review in reviews:
        if not isinstance(review, dict):
            continue
        body = review.get("body")
        if not isinstance(body, str) or marker not in body:
            continue
        user = review.get("user") if isinstance(review.get("user"), dict) else {}
        if str(user.get("login") or "").casefold() != login.casefold():
            continue
        state = str(review.get("state") or "").upper()
        if state != "APPROVED":
            continue
        commit = str(review.get("commit_id") or "")
        if not commit or commit.lower() != head.lower():
            continue
        rev_id = as_int(review.get("id"))
        url = text(review.get("html_url") or review.get("url"))
        if rev_id is None or rev_id <= 0 or url is None:
            continue
        return {
            "id": rev_id,
            "url": url,
            "commit_id": commit,
            "login": login.casefold(),
            "state": "APPROVED",
        }
    return None


def phase_formal_approve(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """APPROVE via review.post activity + commit_id transport; verify strictly."""
    fresh = phase_readiness(store, worker, task, runner)
    if coord(task).get("phase") != "formal_approve":
        return fresh
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    worktree = str(c.get("worktree") or "")
    head = verify_signed_clean_head(store, worker, runner, worktree)
    c["head_sha"] = head
    if number is None or not is_sha(head):
        raise CoordinatorError("formal approve requires PR and head")

    # Fresh exact-head gate/CI checks before action.
    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if not evidence.get("tests_pass") or evidence.get("tests_head") != head:
        raise CoordinatorError("tests not green before formal approve")
    _fresh_ci_still_green(store, worker, task, runner, head)
    latest = latest_gates(store, task["id"])
    for stage, dimension, vendor in GATE_PAIRS:
        g = latest.get((stage, dimension))
        if g is None or g.get("verdict") != "approved" or g.get("head_sha") != head or g.get("vendor") != vendor:
            raise CoordinatorError(f"missing approved gate {stage}/{dimension} before formal approve")

    try:
        review_account = account_for(store, worker.review_session)
        scoped_review = review_account.runner(runner)
    except AccountError as exc:
        raise CoordinatorError(str(exc)) from exc

    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-formal-approve:{task['id']}:{head}"))
    marker = ACTIVITY_MARKER.format(id=activity_id)
    body = f"Formal approval for head `{head[:7]}` after script-verified gates and CI.\n{marker}"

    queue_activity(store, activity_id=activity_id, session_id=worker.review_session,
                   typ="review.post", payload={"repo": target, "number": number,
                                              "body": body, "event": "APPROVE"})
    endpoint = f"repos/{target}/pulls/{number}/reviews"
    def pinned_transport(argv):
        command = list(argv)
        try:
            gh_at = command.index("gh")
        except ValueError:
            return runner(command)
        if command[gh_at:gh_at + 5] == ["gh", "api", "-X", "POST", endpoint]:
            command.extend(["-f", f"commit_id={head}"])
        return runner(command)
    execute_github(store, pinned_transport, activity_ids=(activity_id,))
    recorded = store.row("activity", activity_id)
    if recorded is None or recorded.get("execution_status") != "done":
        raise CoordinatorError("formal approval publication is not verified")
    discovered = _discover_formal_approve(scoped_review, repo=target, number=number,
                                         marker=marker, head=head, login=review_account.login)
    if discovered is None:
        raise CoordinatorError("formal review is not currently APPROVED on the reviewed head")
    recorded["result"] = {"repo": target, "number": number, **discovered}
    store.write("activity", "update", activity_id, strip_row(recorded))
    c["formal_approve_id"] = activity_id
    evidence = c.setdefault("evidence", {})
    if isinstance(evidence, dict):
        evidence["formal_head"] = head
    c["phase"] = "leave_draft"
    save_task(store, task)
    return [f"formal APPROVE on {target}#{number} at {head[:7]}"]


def _task_snapshot(store: Store, tid: str) -> dict[str, Any]:
    task = store.row("task", tid)
    assert task is not None
    checklist = {
        str(r["key"]): str(r["status"])
        for r in store.rows("checklist_item")
        if r.get("task_id") == tid
    }
    checks = [c for c in store.rows("local_check") if c.get("task_id") == tid]
    ordered_checks = sorted(checks, key=lambda c: c.get("ran_at") or "")
    latest_by_name: dict[str, dict[str, Any]] = {}
    for item in ordered_checks:
        name = item.get("name")
        if name is None:
            continue
        latest_by_name[str(name)] = item
    local_checks = [{"name": name, "result": item.get("result")} for name, item in latest_by_name.items()]
    gates_raw = [g for g in store.rows("review_gate") if g.get("task_id") == tid]
    gates_raw.sort(key=lambda g: g.get("recorded_at") or "")
    gates = [
        {
            "stage": g.get("stage"),
            "dimension": g.get("dimension"),
            "vendor": g.get("vendor"),
            "verdict": g.get("verdict"),
            "head_sha": g.get("head_sha") or "",
        }
        for g in gates_raw
    ]
    return {
        "id": task["id"],
        "session_id": task.get("session_id"),
        "workflow": task.get("workflow"),
        "state": task.get("state"),
        "checklist": checklist,
        "summaries": {
            "en": task.get("change_summary_en") or "",
            "de": task.get("change_summary_de") or "",
        },
        "gates": gates,
        "local_checks": local_checks,
    }


def _fresh_formal_still_approved(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    *,
    head: str,
) -> None:
    """Fresh GET: stored formal_head is not current GitHub proof."""
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    if number is None:
        raise CoordinatorError("formal recheck requires PR number")
    try:
        review_account = account_for(store, worker.review_session)
        scoped_review = review_account.runner(runner)
    except AccountError as exc:
        raise CoordinatorError(str(exc)) from exc
    activity_id = c.get("formal_approve_id")
    marker = ACTIVITY_MARKER.format(id=activity_id) if isinstance(activity_id, str) else ""
    discovered = _discover_formal_approve(
        scoped_review,
        repo=target,
        number=number,
        marker=marker or f"Formal approval for head `{head[:7]}`",
        head=head,
        login=review_account.login,
    )
    if discovered is None:
        raise CoordinatorError("formal APPROVE no longer present on exact head (dismissed or missing)")
    if str(discovered.get("state") or "").upper() != "APPROVED":
        raise CoordinatorError("formal review is not APPROVED on fresh GET")
    if str(discovered.get("commit_id") or "").lower() != head.lower():
        raise CoordinatorError("formal APPROVE commit_id mismatch on fresh GET")


def phase_leave_draft(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    worktree = str(c.get("worktree") or "")
    head = verify_signed_clean_head(store, worker, runner, worktree)
    if number is None:
        raise CoordinatorError("leave-draft requires PR number")
    cfg = repo_cfg(worker, str(c["source"]["repo"]))

    # Recheck allow pr-ready and fresh evidence before transition.
    task["state"] = "pr-review"
    save_task(store, task)
    snap = _task_snapshot(store, task["id"])
    allow = evaluate_allow(
        "pr-ready",
        session_id=worker.session_id,
        task_id=task["id"],
        session_tasks=[snap],
    )
    if not allow.allowed:
        raise CoordinatorError(f"pr-ready denied: {allow.reason}")

    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    if evidence.get("formal_head") != head:
        raise CoordinatorError("formal approve not on current head")
    if not evidence.get("tests_pass") or evidence.get("tests_head") != head:
        raise CoordinatorError("tests not green before leave-draft")
    if evidence.get("readiness_head") != head:
        raise CoordinatorError("readiness evidence not on current head before leave-draft")
    _fresh_ci_still_green(store, worker, task, runner, head)
    # Re-run trusted readiness / current-base binding when a prior tick may be stale:
    # formal approval must still be APPROVED on this exact head right now.
    _fresh_formal_still_approved(store, worker, task, runner, head=head)
    latest = latest_gates(store, task["id"])
    for stage, dimension, vendor in GATE_PAIRS:
        g = latest.get((stage, dimension))
        if g is None or g.get("verdict") != "approved" or g.get("head_sha") != head or g.get("vendor") != vendor:
            raise CoordinatorError(f"missing approved gate {stage}/{dimension} before leave-draft")

    scoped_runner = scoped(store, worker.session_id, runner)
    pr = gh_json(
        scoped_runner,
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            target,
            "--json",
            "headRefOid,baseRefName,author,isDraft,state,mergeable",
        ],
    )
    if str(pr.get("headRefOid") or "").lower() != head:
        raise CoordinatorError("PR head mismatch before leave-draft")
    if str(pr.get("mergeable") or "").upper() != "MERGEABLE":
        raise CoordinatorError("PR not mergeable before leave-draft")
    if str(pr.get("baseRefName") or "") != cfg.base:
        raise CoordinatorError("PR base mismatch before leave-draft")
    if pr.get("isDraft") is not True:
        # Already left draft — verify and continue only with matching head.
        if pr.get("isDraft") is False and str(pr.get("state") or "").upper() == "OPEN":
            c["phase"] = "await_merge"
            save_task(store, task)
            return [f"already ready {target}#{number}; awaiting human merge"]
        raise CoordinatorError("PR draft state unexpected before leave-draft")

    fresh = phase_readiness(store, worker, task, runner)
    if coord(task).get("phase") != "formal_approve":
        return fresh
    c["phase"] = "leave_draft"
    save_task(store, task)

    body = (
        f"Ready for review: four lane verdicts approved on `{head[:7]}` "
        f"(grok quality, grok logic, codex quality, codex logic) and CI green. "
        f"Still not merge; a human merges."
    )
    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-ready-comment:{task['id']}:{head}"))
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="comment.post",
        payload={"repo": target, "number": number, "body": body, "target": "pr"},
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    ready_row = store.row("activity", activity_id)
    if ready_row is None or ready_row.get("execution_status") != "done":
        raise CoordinatorError("Ready evidence comment not verified")

    ready = scoped_runner(["gh", "pr", "ready", str(number), "--repo", target])
    if ready.returncode != 0:
        raise CoordinatorError(redact(ready.stderr or ready.stdout or "gh pr ready failed"))
    verify = gh_json(
        scoped_runner,
        ["gh", "pr", "view", str(number), "--repo", target, "--json", "isDraft,headRefOid,state"],
    )
    if verify.get("isDraft") is not False:
        raise CoordinatorError("leave-draft did not clear isDraft")
    if str(verify.get("headRefOid") or "").lower() != head:
        if verify.get("state") == "OPEN" and verify.get("isDraft") is False:
            undone = scoped_runner(["gh", "pr", "ready", str(number), "--repo", target, "--undo"])
            if undone.returncode != 0:
                raise CoordinatorError("PR changed during Ready; returning it to Draft also failed")
        raise CoordinatorError("PR head changed during leave-draft; readiness is not verified")
    if str(verify.get("state") or "").upper() != "OPEN":
        raise CoordinatorError("PR not open after leave-draft")
    c["phase"] = "await_merge"
    c["ready_comment_id"] = activity_id
    if isinstance(evidence, dict):
        evidence["ready_head"] = head
    task["state"] = "pr-review"
    save_task(store, task)
    return [f"left draft {target}#{number}; awaiting human merge"]


def phase_await_merge(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    target = target_repo(task)
    number = as_int(c.get("pr_number"))
    expected_head = str(c.get("head_sha") or "")
    if number is None:
        raise CoordinatorError("await_merge requires PR number")
    scoped_runner = scoped(store, worker.session_id, runner)
    info = gh_json(
        scoped_runner,
        ["gh", "api", f"repos/{target}/pulls/{number}"],
    )
    state = str(info.get("state") or "").upper()
    if info.get("merged") is True:
        sha = str(info.get("merge_commit_sha") or "")
        merged_at = str(info.get("merged_at") or "")
        merged_by = info.get("merged_by") if isinstance(info.get("merged_by"), dict) else None
        if not sha or not merged_at:
            return ["merge observed but incomplete metadata"]
        if merged_by is None:
            return ["merge observed but mergedBy missing"]
        # REQUIRE actual type User from REST pulls API. Never default missing type to human.
        merged_type = str(merged_by.get("type") or "")
        login = str(merged_by.get("login") or "")
        if not login:
            return ["merge observed but mergedBy login missing"]
        if merged_type not in ("User",):
            c["phase"] = "blocked"
            c["blocker"] = f"merge by non-human or unknown actor type ({merged_type or 'missing'})"
            save_task(store, task)
            return publish_blocker(
                store,
                worker,
                task,
                runner,
                f"Merge was not by a human User (got {merged_type or 'missing type'})",
                kind="nonhuman-merge",
            )
        cfg = repo_cfg(worker, str(c["source"]["repo"]))
        if str((info.get("base") or {}).get("ref") or "") != cfg.base:
            raise CoordinatorError("merged PR base mismatch")
        if str((info.get("head") or {}).get("sha") or "") != expected_head:
            raise CoordinatorError("merged PR head differs from reviewed head")
        # Do not manufacture checklist values or boilerplate summaries here.
        # Policy/deviation/implementer keys must already be closed with real evidence
        # before Ready; merge only proves the human merge event.
        if not (task.get("change_summary_en") and task.get("change_summary_de")):
            return [
                "human merge observed; task-done blocked: missing change summaries "
                "describing the actual result"
            ]
        snap = _task_snapshot(store, task["id"])
        allow = evaluate_allow(
            "task-done",
            session_id=worker.session_id,
            task_id=task["id"],
            session_tasks=[snap],
        )
        if not allow.allowed:
            return [f"human merge observed; task-done blocked: {allow.reason} {allow.blocking}"]

        pr_open_id = c.get("pr_open_activity_id")
        mid = str(uuid5(NAMESPACE_URL, f"coordinator-merged:{target}:{number}:{sha}"))
        if store.row("activity", mid) is None:
            store.write(
                "activity",
                "insert",
                mid,
                {
                    "id": mid,
                    "session_id": worker.session_id,
                    "type": "pr.merged",
                    "payload": {
                        "repo": target,
                        "number": number,
                        "url": info.get("html_url") or "",
                        "merge_sha": sha,
                        "merged_at": merged_at,
                        "merged_by": login.casefold(),
                        "merged_by_type": merged_type,
                        "reviewed_head": expected_head,
                        "pr_open_id": pr_open_id,
                    },
                    "execution_status": "done",
                },
            )
        source = c["source"]
        assigned_id = source.get("assigned_id")
        if isinstance(assigned_id, str) and assigned_id:
            ack_id = str(uuid5(NAMESPACE_URL, f"coordinator-ack:{assigned_id}"))
            if store.row("activity", ack_id) is None:
                store.write(
                    "activity",
                    "insert",
                    ack_id,
                    {
                        "id": ack_id,
                        "session_id": worker.session_id,
                        "type": "issue.assigned.ack",
                        "payload": {
                            "assigned_id": assigned_id,
                            "repo": source["repo"],
                            "number": source["number"],
                        },
                        "execution_status": "done",
                    },
                )
        task["state"] = "done"
        c["phase"] = "done"
        save_task(store, task)
        return [f"human merge verified {target}#{number}; task done"]
    if state == "CLOSED":
        c["phase"] = "blocked"
        c["blocker"] = "Ready PR closed without merge"
        save_task(store, task)
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            "Pull request was closed without a verified human merge.",
            kind="closed-unmerged",
        )
    return [f"awaiting human merge of {target}#{number}"]


def phase_read_replies(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    c = coord(task)
    if c.get("uncertain_lane"):
        return [
            "blocked: uncertain prior lane outcome; refusing model start on reply alone"
        ]
    source = c["source"]
    repo = str(source["repo"])
    number = int(source["number"])
    scoped_runner = scoped(store, worker.session_id, runner)
    account = account_for(store, worker.session_id)
    comments = gh_list(
        scoped_runner,
        ["gh", "api", "--paginate", f"repos/{repo}/issues/{number}/comments"],
    )
    allowed = set(worker.reply_logins)
    q_activity = c.get("question_activity_id")
    if not isinstance(q_activity, str) or not q_activity:
        return [f"waiting for question checkpoint on {repo}#{number}"]
    q_marker = f"{QUESTION_MARKER_PREFIX}{q_activity} -->"
    # Also accept ACTIVITY_MARKER form if executor rewrote body.
    q_marker_alt = ACTIVITY_MARKER.format(id=q_activity)

    question_id: int | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body") if isinstance(comment.get("body"), str) else ""
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        login = str(user.get("login") or "").casefold()
        if login != account.login.casefold():
            continue
        if q_marker in body or q_marker_alt in body:
            cid = as_int(comment.get("id"))
            if cid is None:
                continue
            question_id = cid
            break
    if question_id is None:
        return [f"waiting for verified own question comment on {repo}#{number}"]

    consumed = c.get("replies_consumed_through")
    consumed_id = as_int(consumed) if consumed is not None else None
    new_replies: list[str] = []
    last_id = consumed_id if consumed_id is not None else question_id
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        cid = as_int(comment.get("id"))
        if cid is None:
            continue
        # Only replies strictly after the verified own question comment id.
        if cid <= question_id:
            continue
        if consumed_id is not None and cid <= consumed_id:
            continue
        # Valid monotonic ids only.
        if last_id is not None and cid <= last_id:
            continue
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        login = str(user.get("login") or "").casefold()
        if login not in allowed:
            continue
        body = comment.get("body") if isinstance(comment.get("body"), str) else ""
        # Untrusted spec only — no control fields; do not treat copied markers as authority.
        new_replies.append(redact(body, limit=2000))
        last_id = cid

    if not new_replies:
        return [f"waiting for authorized reply on {repo}#{number}"]
    existing = c.get("authorized_replies")
    if not isinstance(existing, list):
        existing = []
    existing.extend(new_replies)
    c["authorized_replies"] = existing
    c["replies_consumed_through"] = last_id
    # Resume the exact safe script phase persisted at the blocker — never blindly
    # start implement for CI authorization / checkout / acceptance administrative issues.
    resume = c.get("resume_phase")
    if isinstance(resume, str) and resume and resume not in ("ask", "blocked", "done"):
        c["phase"] = resume
        c.pop("resume_phase", None)
    else:
        c["phase"] = "implement"
    if c["phase"] == "implement":
        task["state"] = "implementing"
    save_task(store, task)
    return [f"consumed {len(new_replies)} authorized reply(ies); resuming {c['phase']}"]


def phase_blocked(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Externally blocked tasks remain reply-recoverable when outcome is certain."""
    c = coord(task)
    if c.get("uncertain_lane"):
        return publish_blocker(
            store,
            worker,
            task,
            runner,
            str(c.get("blocker") or "uncertain lane outcome"),
            kind="uncertain-lane",
        )
    # Try authorized reply recovery without starting a model blindly.
    prior_phase = str(c.get("phase") or "blocked")
    lines = phase_read_replies(store, worker, task, runner)
    resumed = str(coord(task).get("phase") or "")
    if resumed not in ("ask", "blocked", prior_phase) and resumed:
        return lines
    blocker = redact(str(c.get("blocker") or "blocked"))
    return publish_blocker(store, worker, task, runner, blocker, kind="status")
