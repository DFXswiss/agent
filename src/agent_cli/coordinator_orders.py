"""Local orders of the script-owned coordinator: creation, acceptance, derived state.

The script orchestrates; a model never does.
"""

from __future__ import annotations

import uuid
from typing import Any

from .ai_accounts import AccountError as AIAccountError, load_ai_accounts
from .coordinator_common import (
    CoordinatorError,
    ORDER_KIND,
    Runner,
    account_for,
    coord,
    is_order,
    order_id,
    owned_session,
    redact,
    save_task,
    scoped,
    write_task_text,
)
from .coordinator_config import WorkerConfig, resolve_round_limit
from .coordinator_github import pin_owner_repository, reply_checkpoint_eligible
from .model_slots import model_pins
from .store import Store, utcnow


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
        _require_second_for_worker(ai.models.second, worker.a38)
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
            **({"a38": {"enabled": True}} if worker.a38 else {}),
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
