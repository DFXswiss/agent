"""Local orders of the script-owned coordinator: creation, acceptance, derived state.

The script orchestrates; a model never does.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .ai_accounts import AccountError as AIAccountError, load_ai_accounts
from .coordinator_checks import (
    CheckValue,
    TaskFacts,
    checks_satisfied,
    record_check,
    registry_row_id,
)
from .coordinator_common import (
    CoordinatorError,
    ORDER_KIND,
    Runner,
    account_for,
    as_int,
    comment_target,
    coord,
    is_order,
    order_id,
    owned_session,
    redact,
    save_task,
    scoped,
    strip_row,
    target_repo,
    write_task_text,
)
from .coordinator_config import WorkerConfig, resolve_round_limit
from .coordinator_git import close_pull_request
from .coordinator_github import (
    apply_authorized_replies,
    clear_reply_recovery,
    pin_owner_repository,
    reply_checkpoint_eligible,
)
from .model_slots import model_pins
from .preconditions import CHECKS, PHASE_ACTION
from .store import Store, StoreError, utcnow


ORDER_EVENT = "order.event"
ORDER_REPLY = "order.reply"
WAKE_KINDS = frozenset({"question", "blocked", "ready", "failed", "cancelled"})
EVENT_DIGEST_BOUND = 1500
REFUSAL_BOUND = 300
CANCEL_CLOSE_BODY = "Closing this draft: the order was cancelled."


def serves_repo(worker: WorkerConfig, repo: str) -> bool:
    """True when the repository is an explicit entry or belongs to an approved owner."""
    if any(key.casefold() == repo.casefold() for key in worker.repositories):
        return True
    return repo.split("/", 1)[0].casefold() in worker.repository_owners


def create_order_task(
    store: Store,
    worker: WorkerConfig,
    *,
    repo: str,
    title: str,
    text: str,
    submitted_by: str,
    requested_base: str | None = None,
    notify_session: str | None = None,
    max_rounds: int | None = None,
) -> dict[str, Any]:
    """Insert one queued order task for this worker; returns the stored task row."""
    if not worker.orders:
        raise CoordinatorError(f"worker {worker.session_id} does not accept orders")
    if not serves_repo(worker, repo):
        raise CoordinatorError(f"worker {worker.session_id} does not serve {repo}")

    owned_session(store, worker.session_id)

    from .coordinator_runtime import (
        _discard_task_text,
        _refuse_codex_first,
        _require_second_for_worker,
        execution_binding,
    )

    try:
        ai = load_ai_accounts(store.home)
        _refuse_codex_first(ai.models.first, ai.implementer)
        _require_second_for_worker(
            ai.models.second, worker.a38, worker.a38_second_review
        )
        ai.for_lane(
            worker.session_id, "check", ai.models.first, require_execution=False
        )
        implementer_model = ai.for_lane(
            worker.session_id,
            "implementer",
            ai.models.first,
            require_execution=False,
        ).model

        cfg = next(
            (
                value
                for key, value in worker.repositories.items()
                if key.casefold() == repo.casefold()
            ),
            None,
        )
        if cfg is not None:
            repo = cfg.repo
            if requested_base is not None and requested_base != cfg.base:
                raise CoordinatorError(
                    f"base {requested_base} differs from the configured base "
                    f"{cfg.base} of {repo}"
                )
        login = account_for(store, worker.session_id).login.casefold()
        binding = (
            execution_binding(store, worker, repo, ai, cfg)
            if cfg is not None
            else None
        )
        round_limit = resolve_round_limit(worker, implementer_model, max_rounds)
    except AIAccountError as exc:
        raise CoordinatorError(str(exc)) from exc

    tid = str(uuid.uuid4())
    now = utcnow()
    try:
        sha, chars = write_task_text(worker, tid, text)
        source: dict[str, Any] = {
            "kind": ORDER_KIND,
            "order_id": tid,
            "repo": repo,
            "title": title,
            "body": redact(text, limit=2000),
            "body_sha256": sha,
            "body_chars": chars,
            "submitted_at": now,
            "submitted_by": submitted_by.casefold(),
        }
        if notify_session:
            source["notify_session"] = notify_session
        if cfg is not None:
            source.update(publication_repo=cfg.publication_repo, base=cfg.base)
        else:
            source["owner_scoped"] = True
            source["require_hub_assignment"] = worker.require_hub_assignment
            if requested_base is not None:
                source["requested_base"] = requested_base

        c = {
            "phase": "queued",
            "source": source,
            "branch": f"task-{tid[:8]}",
            **({"execution_binding": binding} if binding is not None else {}),
            "worker_session": worker.session_id,
            "worker_login": login,
            **(
                {
                    "a38": {
                        "enabled": True,
                        **(
                            {"second_review_mode": "model"}
                            if worker.a38_second_review == "model"
                            else {}
                        ),
                    }
                }
                if worker.a38
                else {}
            ),
            "round_limit": round_limit,
        }
        row = {
            "id": tid,
            "session_id": worker.session_id,
            "workflow": "implement",
            "title": f"{worker.session_id[:8]} - {title[:120]}",
            "repo": repo,
            "ref": None,
            "payload": {"coordinator": c},
            "state": "open",
            "current_round": 0,
            **model_pins(ai.models, ai.implementer),
            "created_at": now,
            "updated_at": now,
            "change_summary_en": None,
            "change_summary_de": None,
        }
        store.write("task", "insert", tid, row)
    except BaseException:
        _discard_task_text(worker, tid)
        raise

    return store.row("task", tid)


def phase_accept_order(
    store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner
) -> list[str]:
    """Accept an order whose accept checks are all yes: pin its route and continue to checkout."""
    c = coord(task)
    source = c["source"]
    if source.get("owner_scoped") and not isinstance(
        source.get("repository_config"), dict
    ):
        pinned = pin_owner_repository(
            store,
            worker,
            task,
            runner,
            scoped(store, worker.session_id, runner),
            account_for(store, worker.session_id),
            base=source.get("requested_base"),
        )
        if pinned is not None:
            return pinned
        c = coord(task)
    c["accepted_at"] = utcnow()
    c["phase"] = "checkout"
    save_task(store, task)
    return [f"order accepted {order_id(task)}"]


def order_state(task: dict[str, Any]) -> str:
    """merged | cancelled | failed | queued | waiting | ready | running; derived, never stored."""
    c = coord(task)
    phase = str(c.get("phase") or "accept")
    if task.get("state") == "done":
        return "merged"
    if task.get("state") == "failed" and c.get("failed_kind") == "cancelled":
        return "cancelled"
    if task.get("state") == "failed" or (
        phase == "blocked" and not reply_checkpoint_eligible(task)
    ):
        return "failed"
    if phase == "queued":
        return "queued"
    if phase in ("ask", "blocked") or c.get("binding_blocker"):
        return "waiting"
    evidence = c.get("evidence")
    if isinstance(evidence, dict) and evidence.get("ready_head"):
        return "ready"
    return "running"


def list_orders(store: Store) -> list[dict[str, Any]]:
    """This device's order tasks, oldest first."""
    origin = store.device_id()
    rows = [
        row
        for row in store.rows("task")
        if row.get("_origin_device_id") == origin and is_order(row)
    ]
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        ),
    )


def find_order(store: Store, order_id: str) -> dict[str, Any] | None:
    """The order task with this id when it is this device's row, else None."""
    row = store.row("task", order_id)
    if (
        row is not None
        and row.get("_origin_device_id") == store.device_id()
        and is_order(row)
    ):
        return row
    return None


def order_event_id(order: str, kind: str, occurrence: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"order-event:{order}:{kind}:{occurrence}"))


def own_order_events(store: Store) -> list[dict[str, Any]]:
    """Return this device's order events in their durable event order."""
    rows = store.activity_of_types((ORDER_EVENT,))
    return sorted(
        rows,
        key=lambda row: (
            as_int(row.get("payload", {}).get("seq")) or 0,
            str(row.get("id") or ""),
        ),
    )


def event_answers(
    kind: str, check_key: str | None, check_status: str | None
) -> list[str]:
    if kind in ("ready", "failed", "cancelled"):
        return ["ack"]
    if kind not in ("question", "blocked"):
        return []
    check = CHECKS.get(check_key or "")
    answers: list[str] = []
    if not (
        check is not None
        and check.on_no == "question:override"
        and check_status == "nein"
    ):
        answers.append("reply")
    if check is not None and check.overridable:
        answers.append("override")
    answers.append("cancel")
    return answers


def _event_owned(store: Store, row: dict[str, Any] | None) -> bool:
    return bool(
        row is not None
        and row.get("_origin_device_id") == store.device_id()
        and row.get("type") == ORDER_EVENT
    )


def post_order_event(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    *,
    kind: str,
    occurrence: str,
    digest: str,
    check: str | None = None,
) -> str:
    """Insert one deterministic, sequenced event for an order."""
    event_id = order_event_id(order_id(task), kind, occurrence)

    def existing_result() -> bool:
        existing = store.row("activity", event_id)
        if existing is None:
            return False
        if _event_owned(store, existing):
            return True
        raise CoordinatorError("order event id is held by a foreign row")

    if existing_result():
        return event_id
    with store.exclusive(f"order-event-seq:{store.device_id()}"):
        if existing_result():
            return event_id
        seq = 1 + max(
            (
                as_int(row.get("payload", {}).get("seq")) or 0
                for row in own_order_events(store)
            ),
            default=0,
        )
        c = coord(task)
        source = c.get("source") if isinstance(c.get("source"), dict) else {}
        number = as_int(c.get("pr_number"))
        pr_url = (
            f"https://github.com/{target_repo(task)}/pull/{number}"
            if number is not None
            else None
        )
        check_key = check if isinstance(check, str) and check else None
        check_status = None
        declared = CHECKS.get(check_key or "")
        if declared is not None and declared.fact == "registry":
            fact = store.row(
                "checklist_item", registry_row_id(str(task["id"]), check_key or "")
            )
            if fact is not None:
                check_status = str(fact.get("status") or "") or None
        answers = event_answers(kind, check_key, check_status)
        phase = str(c.get("phase") or "")
        if kind == "blocked" and phase not in ("ask", "blocked"):
            answers = ["cancel"]
        row = {
            "id": event_id,
            "session_id": worker.session_id,
            "type": ORDER_EVENT,
            "execution_status": "done",
            "payload": {
                "order_id": order_id(task),
                "kind": kind,
                "wake": kind in WAKE_KINDS,
                "seq": seq,
                "digest": redact(digest, limit=EVENT_DIGEST_BOUND),
                "pr_url": pr_url,
                "phase": phase,
                "round": as_int(task.get("current_round")) or 0,
                "check": check_key,
                "answers": answers,
                "failed_kind": c.get("failed_kind") if kind == "failed" else None,
                "notify_session": source.get("notify_session"),
                "at": utcnow(),
                "ack": None,
            },
        }
        store.write("activity", "insert", event_id, row)
    return event_id


def acknowledge_event(store: Store, event_id: str, *, by: str, via: str) -> bool:
    row = store.row("activity", event_id)
    if not _event_owned(store, row):
        return False
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("ack"):
        return False
    payload["ack"] = {"at": utcnow(), "by": by, "via": via}
    store.write("activity", "update", event_id, strip_row(row))
    return True


def _commit_order(store: Store, task: dict[str, Any], change, *also) -> None:
    """Save a changed copy of the task, and the given row writes, in one transaction."""
    committed = copy.deepcopy(task)
    change(committed)
    with store.conn.transaction():
        save_task(store, committed)
        for write in also:
            write()
    task.clear()
    task.update(committed)


def reconcile_order_events(
    store: Store, worker: WorkerConfig, task: dict[str, Any]
) -> None:
    """Create and reconcile every event implied by the saved order state."""
    c = coord(task)
    stored_order = c.get("order")
    o = copy.deepcopy(stored_order) if isinstance(stored_order, dict) else {}
    phase = str(c.get("phase") or "")
    ended = task.get("state") in ("done", "failed")
    number = as_int(c.get("pr_number"))
    url = (
        f"https://github.com/{target_repo(task)}/pull/{number}"
        if number is not None
        else "-"
    )

    def post(
        *, kind: str, occurrence: str, digest: str, check: str | None = None
    ) -> str:
        return post_order_event(
            store,
            worker,
            task,
            kind=kind,
            occurrence=occurrence,
            digest=digest,
            check=check,
        )

    def acknowledge_checkpoint(via: str) -> None:
        checkpoint = o.get("checkpoint")
        event_id = checkpoint.get("event_id") if isinstance(checkpoint, dict) else None
        if isinstance(event_id, str):
            acknowledge_event(
                store, event_id, by=worker.session_id, via=via
            )

    if c.get("accepted_at"):
        post(
            kind="accepted",
            occurrence="",
            digest=f"Order accepted for {target_repo(task)}.",
        )
    if number is not None:
        post(
            kind="draft-opened",
            occurrence=str(number),
            digest=f"Draft pull request opened: {url}",
        )

    if ended:
        if isinstance(o.get("checkpoint"), dict):
            acknowledge_checkpoint("superseded")
            o.pop("checkpoint", None)
        o.pop("refused", None)
        if task.get("state") == "done":
            post(
                kind="ready",
                occurrence=str(c.get("head_sha") or ""),
                digest=f"Pull request left draft and was merged by a human: {url}",
            )
            post(
                kind="merged",
                occurrence="",
                digest=f"Pull request merged: {url}",
            )
        elif c.get("failed_kind") == "cancelled":
            post(
                kind="cancelled",
                occurrence="",
                digest=f"Order cancelled: {c.get('blocker') or ''}",
            )
        else:
            kind_text = str(c.get("failed_kind") or "failed")
            post(
                kind="failed",
                occurrence=kind_text,
                digest=f"Order failed ({kind_text}): {c.get('blocker') or ''}",
                check=c.get("failed_check"),
            )
    elif phase in ("ask", "blocked") or c.get("binding_blocker"):
        if order_state(task) == "failed":
            kind = "failed"
        elif phase == "ask" or (
            phase == "blocked"
            and isinstance(c.get("question_text"), str)
            and c.get("question_text")
        ):
            kind = "question"
        else:
            kind = "blocked"
        key = "|".join(
            [
                kind,
                str(c.get("resume_phase") or ""),
                str(c.get("failed_check") or ""),
                "binding" if c.get("binding_blocker") else "",
            ]
        )
        checkpoint = o.get("checkpoint")
        if not isinstance(checkpoint, dict) or checkpoint.get("key") != key:
            if isinstance(checkpoint, dict):
                acknowledge_checkpoint("superseded")
            seq = int(o.get("checkpoint_seq") or 0) + 1
            if kind == "question":
                digest = str(c.get("question_text") or c.get("blocker") or "")
            elif kind == "blocked":
                text = (
                    c.get("blocker")
                    if phase == "blocked" and c.get("blocker")
                    else c.get("binding_blocker") or c.get("blocker") or ""
                )
                digest = f"Blocked: {text}"
            else:
                digest = f"Order failed (blocked): {c.get('blocker') or ''}"
            if o.get("refused"):
                digest = f"Previous answer refused: {o['refused']}\n{digest}"
            event_id = post(
                kind=kind,
                occurrence=str(seq),
                digest=digest,
                check=c.get("failed_check"),
            )
            o["checkpoint_seq"] = seq
            o["checkpoint"] = {"event_id": event_id, "kind": kind, "key": key}
            o.pop("refused", None)
    else:
        o.pop("refused", None)
        checkpoint = o.get("checkpoint")
        if isinstance(checkpoint, dict):
            checkpoint_id = str(checkpoint.get("event_id") or "")
            post(
                kind="resumed",
                occurrence=checkpoint_id,
                digest=f"Resumed in phase {phase}.",
            )
            acknowledge_checkpoint("resumed")
            o.pop("checkpoint", None)
        evidence = c.get("evidence")
        ready_head = (
            evidence.get("ready_head") if isinstance(evidence, dict) else None
        )
        if ready_head:
            post(
                kind="ready",
                occurrence=str(ready_head),
                digest=f"Pull request is ready for human review and merge: {url}",
            )

    if o != (stored_order if isinstance(stored_order, dict) else {}):
        _commit_order(
            store,
            task,
            lambda changed: coord(changed).__setitem__("order", o),
        )


def sweep_terminal_orders(store: Store, worker: WorkerConfig) -> list[str]:
    lines: list[str] = []
    origin = store.device_id()
    for task in store.rows("task"):
        if (
            task.get("_origin_device_id") != origin
            or task.get("session_id") != worker.session_id
            or not is_order(task)
            or task.get("state") not in ("done", "failed")
        ):
            continue
        order_data = coord(task).get("order")
        if isinstance(order_data, dict) and order_data.get("closed"):
            continue
        try:
            reconcile_order_events(store, worker, task)

            def close_order(changed: dict[str, Any]) -> None:
                coord(changed).setdefault("order", {})["closed"] = True

            _commit_order(
                store,
                task,
                close_order,
            )
        except StoreError as exc:
            lines.append(f"store error: {redact(str(exc))}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"error: {redact(str(exc))}")
    return lines


def pending_order_intents(
    store: Store, worker: WorkerConfig
) -> dict[str, list[dict[str, Any]]]:
    rows = []
    for row in store.activity_of_types((ORDER_REPLY,)):
        payload = row.get("payload")
        if (
            row.get("execution_status") == "pending"
            and row.get("session_id") == worker.session_id
            and isinstance(payload, dict)
            and isinstance(payload.get("order_id"), str)
        ):
            rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row["payload"].get("created_at") or ""),
            str(row.get("id") or ""),
        )
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["payload"]["order_id"], []).append(row)
    return grouped


def latest_order_intent(store: Store, order: str) -> dict[str, Any] | None:
    matches = [
        row
        for row in store.activity_of_types((ORDER_REPLY,))
        if isinstance(row.get("payload"), dict)
        and row["payload"].get("order_id") == order
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda row: (
            str(row["payload"].get("created_at") or ""),
            str(row.get("id") or ""),
        ),
    )


def _finish_intent(
    store: Store,
    intent: dict[str, Any],
    *,
    effect: str | None = None,
    error: str | None = None,
) -> None:
    intent_id = str(intent.get("id") or "")
    row = store.row("activity", intent_id)
    if (
        row is None
        or row.get("_origin_device_id") != store.device_id()
        or row.get("type") != ORDER_REPLY
        or row.get("execution_status") != "pending"
    ):
        return
    if error is None:
        row["execution_status"] = "done"
        row["result"] = {"consumed_at": utcnow(), "effect": effect}
    else:
        row["execution_status"] = "error"
        row["execution_error"] = error
    store.write("activity", "update", intent_id, strip_row(row))


def consume_order_intents(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    intents: list[dict[str, Any]],
) -> list[str] | None:
    """Consume ordered local replies, overrides, and cancellations for one order."""
    lines: list[str] = []
    oid = order_id(task)

    def refuse(intent: dict[str, Any], message: str) -> None:
        payload = intent.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        c = coord(task)
        order_data = c.get("order")
        checkpoint = (
            order_data.get("checkpoint") if isinstance(order_data, dict) else None
        )
        event_id = (
            checkpoint.get("event_id") if isinstance(checkpoint, dict) else None
        )
        if (
            str(c.get("phase") or "") in ("ask", "blocked")
            and isinstance(event_id, str)
            and payload.get("answers") == event_id
        ):
            def change(changed: dict[str, Any]) -> None:
                changed_order = coord(changed).setdefault("order", {})
                changed_order["refused"] = redact(message, limit=REFUSAL_BOUND)
                changed_order.pop("checkpoint", None)

            _commit_order(
                store,
                task,
                change,
                lambda: acknowledge_event(
                    store,
                    event_id,
                    by=worker.session_id,
                    via="superseded",
                ),
                lambda: _finish_intent(store, intent, error=message),
            )
            lines.append(f"answer refused for order {oid}: {message}")
        else:
            _finish_intent(store, intent, error=message)

    for intent in intents:
        payload = intent.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        by = str(payload.get("by") or "")
        c = coord(task)
        if task.get("state") in ("done", "failed"):
            _finish_intent(
                store,
                intent,
                error=f"order is already {order_state(task)}",
            )
            continue
        kind = payload.get("kind")
        if kind not in ("reply", "override", "cancel"):
            refuse(intent, "unknown intent kind")
            continue

        if kind == "cancel":
            if checks_satisfied(TaskFacts(store, task), task, "close_draft"):
                outcome = close_pull_request(
                    store,
                    worker,
                    task,
                    runner,
                    activity_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"coordinator-cancel-close:{task['id']}",
                        )
                    ),
                    body=CANCEL_CLOSE_BODY,
                )
                if outcome == "MERGED":
                    refuse(intent, "pull request already merged")
                    continue

            def cancel(changed: dict[str, Any]) -> None:
                changed["state"] = "failed"
                changed_c = coord(changed)
                changed_c["phase"] = "blocked"
                clear_reply_recovery(changed_c)
                changed_c["failed_kind"] = "cancelled"
                changed_c["blocker"] = redact(
                    str(payload.get("reason") or ""), limit=500
                )
                changed_c.pop("closing_draft", None)
                changed_c.pop("retry", None)

            _commit_order(
                store,
                task,
                cancel,
                lambda: _finish_intent(store, intent, effect="cancel"),
            )
            lines.append(f"order cancelled {oid}")
            continue

        if kind == "override":
            key = str(payload.get("check") or "")
            check = CHECKS.get(key)
            reason = str(payload.get("reason") or "")
            row = store.row(
                "checklist_item", registry_row_id(str(task["id"]), key)
            )
            already_written = bool(
                row is not None
                and row.get("status") == "ja"
                and isinstance(row.get("override"), dict)
                and row["override"].get("intent_id") == intent.get("id")
            )
            if not already_written:
                if check is None or not check.overridable:
                    refuse(intent, f"check {key} cannot be overridden")
                    continue
                if row is None or row.get("status") not in ("nein", "unavailable"):
                    refuse(intent, f"check {key} is not failed")
                    continue
                action = PHASE_ACTION.get(
                    str(c.get("resume_phase") or c.get("phase") or "")
                )
                if action is None:
                    refuse(intent, f"check {key} cannot be overridden")
                    continue
            else:
                action = None

            committed = copy.deepcopy(task)
            committed_c = coord(committed)
            try:
                with store.conn.transaction():
                    if not already_written:
                        record_check(
                            store,
                            committed,
                            check,
                            CheckValue(
                                "ja",
                                f"override by {by}: {reason}",
                                source="human",
                            ),
                            action=action,
                            override={
                                "by": by,
                                "reason": reason,
                                "at": utcnow(),
                                "intent_id": intent.get("id"),
                            },
                        )
                    resume = committed_c.get("resume_phase")
                    if (
                        committed_c.get("failed_check") == key
                        and committed_c.get("phase") in ("ask", "blocked")
                        and isinstance(resume, str)
                        and resume
                    ):
                        committed_c["phase"] = resume
                        for field in (
                            "resume_phase",
                            "failed_check",
                            "question_text",
                            "question_activity_id",
                            "replies_consumed_through",
                            "blocker",
                        ):
                            committed_c.pop(field, None)
                        if resume == "implement":
                            committed["state"] = "implementing"
                    save_task(store, committed)
                    _finish_intent(store, intent, effect="override")
            except CoordinatorError:
                refuse(intent, f"check {key} cannot be overridden")
                continue
            task.clear()
            task.update(committed)
            lines.append(f"check {key} overridden by {by}")
            continue

        c = coord(task)
        if (
            str(c.get("phase") or "") not in ("ask", "blocked")
            or not reply_checkpoint_eligible(task)
        ):
            refuse(intent, "no pending question")
            continue
        order_data = c.get("order")
        checkpoint = (
            order_data.get("checkpoint") if isinstance(order_data, dict) else None
        )
        current = checkpoint.get("event_id") if isinstance(checkpoint, dict) else None
        if not isinstance(current, str) or payload.get("answers") != current:
            refuse(intent, "stale reply")
            continue
        failed_key = str(c.get("failed_check") or "")
        failed_check = CHECKS.get(failed_key)
        reset_unavailable = False
        if failed_check is not None and failed_check.on_no == "question:override":
            failed_row = store.row(
                "checklist_item",
                registry_row_id(str(task["id"]), failed_key),
            )
            failed_status = failed_row.get("status") if failed_row is not None else None
            if failed_status == "nein":
                refuse(intent, f"check {failed_key} needs an override")
                continue
            reset_unavailable = failed_status == "unavailable"
        if (
            c.get("blocker_publish_once") is True
            and comment_target(task) is not None
            and not (
                isinstance(c.get("blocker_activity_id"), str)
                and c.get("blocker_activity_id")
            )
        ):
            return lines or None

        committed = copy.deepcopy(task)
        committed_c = coord(committed)
        with store.conn.transaction():
            if reset_unavailable and failed_check is not None:
                action = PHASE_ACTION.get(str(committed_c.get("resume_phase") or ""))
                if action is not None:
                    record_check(
                        store,
                        committed,
                        failed_check,
                        CheckValue("pending", f"re-run requested by {by}"),
                        action=action,
                    )
            reply_lines = apply_authorized_replies(
                store,
                committed,
                [redact(str(payload.get("body") or ""), limit=2000)],
            )
            _finish_intent(store, intent, effect="reply")
        task.clear()
        task.update(committed)
        lines.extend(reply_lines)

    return lines or None
