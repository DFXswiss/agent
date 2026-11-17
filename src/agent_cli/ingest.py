"""Turn a GitHub mention into a runner job.

No network of its own: `gh` runs through the injected runner, the way `watch.py`
does it. Whether a request is admitted is decided in `jobs.py`; this module reads
notifications, works out what was asked for, and writes the row.

Every step is fail-closed. A notification that cannot be parsed, a body that
cannot be read, a policy that does not name the actor — each ends the request
rather than widening it. The cost of skipping one mention is that somebody asks
again; the cost of admitting one wrongly is a bot acting on a stranger's word.

What a mention asks for comes from the skills catalogue: command prefixes and
body phrases name a skill, and that skill's queue type is what admission
allows. A request that resolves to a skill this instance does not run is
denied; it is not replaced by the default. No match at all falls back to the
policy's `default_skill_on_mention`.

A notification that led to a job row is marked read. GitHub keeps an unread
notification in the list until a client marks the thread read, so without this
the only thing preventing a second job would be the job row itself — and
`prune` deletes finished rows once their retention has passed, which would let
the same, already handled request be queued again. A later mention makes the
thread unread again and is evaluated afresh: a new comment can retry a failed
job, or start a new generation of a finished pull request whose head moved; a
mention of a job that is still queued or running, or one already handled from
this comment or an earlier one, is a duplicate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .jobs import Verdict, admits, job_id
from .queue import retry_payload
from .runtime import Completed
from .store import Store, StoreConnectionError, utcnow

# A mention inside a markdown quote is a citation, not an address. Someone
# quoting an earlier request must not trigger a second run of it.
_QUOTE = re.compile(r"^\s*>.*$", re.MULTILINE)
# The same holds for code: a handle shown in a fenced block or an inline span is
# an example being displayed, not somebody being addressed. Documenting how to
# call this bot would otherwise call it.
_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_CODE_SPAN = re.compile(r"`[^`\n]*`")
# GitHub sends a notification's thread id as a string of ASCII digits. Only
# that shape may become part of a request path.
_DIGITS = re.compile(r"^[0-9]+$")
# A handle is `@` followed by the characters GitHub logins use. It is removed
# before a skill is resolved: the person addressed is not part of what was
# asked for.
_HANDLE = re.compile(r"@[a-z0-9-]+")
# A pull request's head is a full lower-case object id, and nothing else counts
# as a known head. A short, upper-case or empty answer would make a `done` row
# look changed (or unchanged) on a guess.
_HEAD_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Skill:
    """One catalogue entry reduced to what ingest decides with.

    `queue_type` is what admission compares with `job_types_allow`; the id is
    what names the job and selects the worker and its `runner.json` settings.
    An entry without a usable queue type keeps `None` and is therefore never
    admitted.
    """

    id: str
    queue_type: str | None


class ListingFailed(Exception):
    """The notifications listing could not be read as a list of pages.

    This is not "no notifications". A failed `gh` call, unreadable JSON, or a
    JSON value that is not a list of pages leaves every thread undecided. The
    caller must report the pass as failed rather than claim an empty queue.
    """


def _addressing_text(body: str) -> str:
    """`body` with everything that quotes or displays rather than says it removed."""
    without_code = _CODE_SPAN.sub("", _FENCE.sub("", body))
    return _QUOTE.sub("", without_code).lower()


def mentions(body: Any, login: str) -> bool:
    """Whether `body` addresses `login`, ignoring quotes and code."""
    if not isinstance(body, str) or not isinstance(login, str) or not login:
        return False
    stripped = _addressing_text(body)
    # The trailing group stops `@theo-vane-bot` from answering for `@theo-vane`.
    # The leading group stops a handle sitting at the end of a longer word — an
    # e-mail address — from counting as an address either.
    return re.search(rf"(^|[^a-z0-9-])@{re.escape(login.lower())}([^a-z0-9-]|$)", stripped) is not None


def catalog_skills(catalog: Any) -> list[dict[str, Any]] | None:
    """The catalogue's skill entries, or None when it cannot be read.

    `skills` may be a list or a mapping; a mapping is read by its values, the
    same way `health.unresolved_skills` reads it. `None` entries are dropped —
    they are how a list stays sparse — while any other non-dict entry makes the
    whole catalogue unreadable: silently skipping it would hide a skill that
    exists but that this module cannot see. Pure: the caller supplies the
    parsed catalogue.
    """
    if not isinstance(catalog, dict):
        return None
    raw = catalog.get("skills")
    if isinstance(raw, list):
        entries: list[Any] = list(raw)
    elif isinstance(raw, dict):
        entries = list(raw.values())
    else:
        return None
    out: list[dict[str, Any]] = []
    for entry in entries:
        if entry is None:
            continue
        if not isinstance(entry, dict):
            return None
        out.append(entry)
    return out


def _skill_of(entry: Any) -> Skill | None:
    """One catalogue entry as a `Skill`, or None when it names nothing.

    Only a canonical id names a skill: a non-empty string equal to
    `id.strip().lower()`. The id is used as written — it becomes the row's
    `job_type` and the key `finalize.done_kind` and `_budget` look up in
    `runner.json`, and `health.unresolved_skills` checks the catalogue id as
    written — so an id in any other spelling would never find its own entry.
    An upper-case or padded id therefore names no skill, exactly as a missing
    id does: `resolve_skill`, `default_skill` and `allowed_skills` ignore it.
    """
    if not isinstance(entry, dict):
        return None
    raw_id = entry.get("id")
    if (
        not isinstance(raw_id, str)
        or not raw_id
        or raw_id != raw_id.strip().lower()
    ):
        return None
    raw_queue = entry.get("queue_type")
    queue_type = (
        raw_queue if isinstance(raw_queue, str) and raw_queue.strip() else None
    )
    return Skill(id=raw_id, queue_type=queue_type)


def _patterns(entry: dict[str, Any], key: str) -> list[str]:
    """A catalogue pattern list, lower-cased; anything unusable is dropped."""
    raw = entry.get(key)
    if not isinstance(raw, list):
        return []
    return [item.lower() for item in raw if isinstance(item, str) and item.strip()]


def _body_pattern(pattern: str) -> re.Pattern[str]:
    """A word-bounded matcher for one pattern.

    The existing runner used a plain substring search; this requires the
    pattern to stand as a token, so `ptal` does not match `capital`. A hyphen
    is part of a token here, matching the bound `mentions` uses.
    """
    return re.compile(rf"(^|[^a-z0-9-]){re.escape(pattern)}([^a-z0-9-]|$)")


def resolve_skill(body: str, catalog: Any) -> Skill | None:
    """The catalogue skill a mention asks for, or None when none matches.

    Resolution uses the addressing text — quotes and code removed, lower-cased
    — with every `@handle` removed. Two kinds of pattern are compared: a
    command prefix against the first whitespace-separated word of the first
    non-empty line (startswith, case-insensitive), and every body pattern plus
    every non-slash command prefix against the whole text on word boundaries.
    The longest matching pattern wins, and a tie goes to the lower skill id.

    The word-boundary rule is the one deliberate difference from the runner
    being replaced, which searched for a plain substring: `ptal` must not be
    found inside `capital`. An unknown request is not replaced by the default
    here; the caller decides whether a default applies.
    """
    if not isinstance(body, str) or not body:
        return None
    entries = catalog_skills(catalog)
    if entries is None:
        return None
    text = _HANDLE.sub(" ", _addressing_text(body))
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    words = first_line.split()
    first_word = words[0] if words else ""

    best: tuple[int, str, Skill] | None = None
    for entry in entries:
        skill = _skill_of(entry)
        if skill is None:
            continue
        prefixes = _patterns(entry, "command_prefixes")
        body_patterns = _patterns(entry, "body_patterns")
        scores: list[int] = []
        for prefix in prefixes:
            if first_word and first_word.startswith(prefix):
                scores.append(len(prefix))
        for pattern in [*body_patterns, *prefixes]:
            if pattern.startswith("/"):
                # A slash command is a command, not a phrase in running text.
                continue
            if _body_pattern(pattern).search(text):
                scores.append(len(pattern))
        if not scores:
            continue
        candidate = (max(scores), skill.id.lower(), skill)
        if best is None or candidate[0] > best[0] or (
            candidate[0] == best[0] and candidate[1] < best[1]
        ):
            best = candidate
    return None if best is None else best[2]


def default_skill(policy: Any, catalog: Any) -> Skill | None:
    """The catalogue skill named by `default_skill_on_mention`, or None."""
    if not isinstance(policy, dict):
        return None
    raw = policy.get("default_skill_on_mention")
    if not isinstance(raw, str) or not raw.strip():
        return None
    wanted = raw.strip().lower()
    for entry in catalog_skills(catalog) or []:
        skill = _skill_of(entry)
        if skill is not None and skill.id.lower() == wanted:
            return skill
    return None


def allowed_skills(policy: Any, catalog: Any) -> tuple[list[Skill], list[str]]:
    """Catalogue skills whose queue type the policy allows, and unknown types.

    Admission compares a skill's *queue type* with `job_types_allow`, so the
    worker check has to make the same translation: an allowed queue type names
    the skill whose worker must exist. An allowed queue type that no catalogue
    skill carries is returned separately; `doctor` reports it and the dispatch
    preflight refuses it. Pure, so both callers apply one mapping.
    """
    skills = [
        skill
        for entry in catalog_skills(catalog) or []
        if (skill := _skill_of(entry)) is not None
    ]
    raw = policy.get("job_types_allow") if isinstance(policy, dict) else None
    if not isinstance(raw, list):
        return [], []
    matched: list[Skill] = []
    unknown: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        wanted = item.lower()
        found = [
            skill
            for skill in skills
            if skill.queue_type is not None
            and skill.queue_type.lower() == wanted
        ]
        if found:
            matched.extend(found)
        else:
            unknown.append(item)
    return matched, unknown


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def trigger_comment_id(trigger: Any) -> int | None:
    """The comment id at the end of a trigger string, or None when unusable.

    A trigger is `comment:<repo>:<ref>:<id>`; the id is the last
    `:`-separated part and must be ASCII digits. Nothing else parses, so the
    caller treats an unparsable value like a trigger that was never recorded.
    """
    if not isinstance(trigger, str):
        return None
    last = trigger.rsplit(":", 1)[-1]
    if _DIGITS.fullmatch(last) is None:
        return None
    return int(last)


def notification_request(item: Any) -> tuple[str, str] | None:
    """The (repo, number) a notification points at, or None when unusable."""
    if not isinstance(item, dict):
        return None
    subject = item.get("subject")
    repository = item.get("repository")
    if not isinstance(subject, dict) or not isinstance(repository, dict):
        return None
    full = repository.get("full_name")
    if not isinstance(full, str) or full.count("/") != 1 or "" in full.split("/"):
        return None
    url = subject.get("url")
    if not isinstance(url, str):
        return None
    number = _int(url.rsplit("/", 1)[-1])
    if number is None or number <= 0:
        return None
    return full, str(number)


def notification_thread_id(item: Any) -> str | None:
    """The notification's thread id, or None when it carries no usable one.

    GitHub sends the id as a string of digits. Anything else cannot be put into
    a `gh api` path without inventing a value, so it counts as absent: the job
    is still created, only the mark-read call is skipped.
    """
    if not isinstance(item, dict):
        return None
    value = item.get("id")
    if isinstance(value, str) and _DIGITS.fullmatch(value) is not None:
        return value
    return None


def mark_read_argv(thread_id: str) -> list[str]:
    """The `gh` call that marks one notification thread read.

    Raises `ValueError` for anything but ASCII digits: the id becomes part of
    the request path, so a value with a slash in it would address a different
    endpoint.
    """
    if not isinstance(thread_id, str) or _DIGITS.fullmatch(thread_id) is None:
        raise ValueError(f"notification thread id must be digits: {thread_id!r}")
    return ["gh", "api", "-X", "PATCH", f"notifications/threads/{thread_id}"]


def _mark_thread_read(item: Any, runner: Callable[[list[str]], Completed]) -> None:
    """Mark the notification's thread read; absence or failure is not fatal.

    A notification without a usable id is left alone. A failed call is ignored:
    the job row still de-duplicates until prune, and the next pass tries again.
    """
    thread_id = notification_thread_id(item)
    if thread_id is None:
        return
    runner(mark_read_argv(thread_id))


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _consume_mention(
    store: Store,
    row_id: str,
    row: dict[str, Any],
    trigger: str,
    item: Any,
    runner: Callable[[list[str]], Completed],
) -> bool:
    """Record a handled comment on its row, then mark the thread read.

    `Store.write` replaces the whole row, so every stored field is kept and
    only the driver's `_` keys are dropped. The trigger is written first: a
    mention handled without a new job still has to be recorded, or a later
    failure or head change that makes the thread unread again selects the
    same comment and queues it a second time. A retryable write failure leaves
    the thread unread and reports us as skipped, so the next pass tries again;
    a `StoreError` raised for a data problem is not retryable and propagates.

    Every caller has already established that the mention's trigger differs
    from the stored one, so the write is unconditional.

    `ingest` holds the runner lock, which every command that writes job rows
    shares, so this read-modify-write cannot race them.
    """
    updated = _strip(row)
    updated["trigger"] = trigger
    updated["updated_at"] = utcnow()
    try:
        store.write("job", "update", row_id, updated)
    except (Exception, StoreConnectionError):
        # A lost connection is retryable and an ordinary exception is a failed
        # pass. Any other StoreError is a genuine data problem and must fail
        # loud rather than be reported as a skip.
        return False
    _mark_thread_read(item, runner)
    return True


def job_row(
    *,
    session_id: str,
    repo: str,
    ref: str,
    job_type: str,
    actor: str,
    queue_type: str | None = None,
    trigger: str | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """A fresh job row in `queued`.

    `job_type` is the skill id. `queue_type` records what admission compared,
    `trigger` the comment that caused the row, and `head_sha` the pull request
    head it was created for (None for an issue, or when the head was unknown).
    The last three default to None so callers that only name a job keep working.
    """
    now = utcnow()
    return {
        "id": job_id(repo, ref, job_type),
        "session_id": session_id,
        "repo": repo,
        "ref": ref,
        "job_type": job_type,
        "queue_type": queue_type,
        "trigger": trigger,
        "head_sha": head_sha,
        "actor": actor,
        "state": "queued",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }


def _comment_author(comment: Any) -> str | None:
    """The login that wrote a comment, or None when it carries no usable one."""
    if not isinstance(comment, dict):
        return None
    user = comment.get("user")
    return user.get("login") if isinstance(user, dict) else None


def _is_own_comment(comment: Any, login: str) -> bool:
    """Whether the comment was written by this runner's own login."""
    author = _comment_author(comment)
    return isinstance(author, str) and author.lower() == login.lower()


def _head_sha(
    repo: str, ref: str, runner: Callable[[list[str]], Completed]
) -> str | None:
    """The pull request's current head, or None when it cannot be known.

    An issue, a failed call, or an answer that is not a full lower-case object
    id all leave the head unknown. That is the fail-closed answer: an unknown
    head must not make a finished job look changed.
    """
    completed = runner(
        ["gh", "api", f"repos/{repo}/pulls/{ref}", "--jq", ".head.sha"]
    )
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip()
    if _HEAD_SHA.fullmatch(value) is None:
        return None
    return value


def scan_mentions(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    session_id: str,
    login: str,
    policy: Any,
    catalog: Any,
    device_id: str,
    requeue: Callable[[str, dict[str, Any], dict[str, Any]], bool],
) -> tuple[list[str], list[str], int]:
    """Insert or re-queue a job for every notification this instance admits.

    Returns the ids created, the ids re-queued, and the number of notifications
    skipped. A mention is identified by its comment (`trigger`), not by its
    thread, so a new comment on an already handled pull request is a new
    request.

    What a mention asks for is resolved through the skills catalogue; the
    policy's `default_skill_on_mention` supplies the skill when no pattern
    matches. Admission compares that skill's queue type with
    `job_types_allow`, so a request for a skill this instance does not run
    stays denied instead of falling back to the default.

    An existing row is handled according to its state: `queued`/`running` are
    left alone, `failed` is retried, and `done` becomes a new generation only
    when it is a pull request whose head moved. The recorded trigger is a
    high-water mark: a comment at or before it was handled already, so only a
    newer comment reaches the state handling. A row with no recorded trigger,
    or one that does not parse, is treated as already handled and records the
    comment, so an old request is never retried merely because the thread
    changed.

    A notification that produced a job — inserted, re-queued, or already
    handled — is marked read, so its de-duplication does not depend on the job
    row outliving its retention. A comment at or before the recorded trigger is
    only marked read. On the other handled paths the comment is recorded as the
    row's `trigger` first, so a later failure or head change that makes the
    thread unread cannot select the same mention again. Every skip path leaves
    the thread unread, so the next pass re-evaluates it exactly as before.
    """
    created: list[str] = []
    requeued: list[str] = []
    skipped = 0

    completed = runner(["gh", "api", "--paginate", "--slurp", "notifications"])
    if completed.returncode != 0:
        raise ListingFailed("notifications listing unusable")
    try:
        pages = json.loads(completed.stdout or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ListingFailed("notifications listing unusable") from exc
    # `--slurp` returns one list per page. Any other shape means the request
    # path or the output cannot be trusted, so no thread is admitted and none
    # is marked read.
    if not isinstance(pages, list) or any(
        not isinstance(page, list) for page in pages
    ):
        raise ListingFailed("notifications listing unusable")
    items = [i for page in pages for i in page]

    for item in items:
        target = notification_request(item)
        if target is None:
            skipped += 1
            continue
        repo, ref = target

        info = runner(
            ["gh", "api", f"repos/{repo}/issues/{ref}/comments", "--paginate", "--slurp"]
        )
        if info.returncode != 0:
            skipped += 1
            continue
        try:
            comment_pages = json.loads(info.stdout or "[]")
        except json.JSONDecodeError:
            skipped += 1
            continue
        comments = [c for page in comment_pages if isinstance(page, list) for c in page]

        # The bot's own comment is never a request, even when it quotes the
        # handle. Dropping it before the last-addressing choice also stops it
        # from hiding an earlier human request.
        others = [
            c
            for c in comments
            if isinstance(c, dict) and not _is_own_comment(c, login)
        ]
        addressed = [c for c in others if mentions(c.get("body"), login)]
        if not addressed:
            skipped += 1
            continue
        last = addressed[-1]
        actor = _comment_author(last)
        if not isinstance(actor, str) or not actor:
            skipped += 1
            continue
        comment_id = last.get("id")
        if isinstance(comment_id, bool) or not isinstance(comment_id, int):
            skipped += 1
            continue
        trigger = f"comment:{repo}:{ref}:{comment_id}"

        skill = resolve_skill(last.get("body"), catalog) or default_skill(
            policy, catalog
        )
        if skill is None:
            skipped += 1
            continue
        # `_skill_of` admits only a canonical id, so `skill.id` is already the
        # spelling `done_kind` and the `runner.json` keys use.
        job_type = skill.id

        private = bool(_repo_is_private(repo, runner))
        verdict: Verdict = admits(
            policy,
            actor=actor,
            repo=repo,
            job_type=skill.queue_type or "",
            private=private,
        )
        if not verdict.admitted:
            skipped += 1
            continue

        row_id = job_id(repo, ref, job_type)
        existing = store.row("job", row_id)

        if existing is not None:
            if existing.get("id") != row_id:
                # The row was stored under this key but carries another id. Its
                # id is what the cleanup and the worker use as a path, so acting
                # on it could touch files the key does not name. Leave the
                # thread unread for a pass that can see a consistent row.
                skipped += 1
                continue
            if existing.get("_origin_device_id") != device_id:
                # Another device owns this job; do not act on its row and leave
                # the thread alone so that device's pass can decide.
                skipped += 1
                continue
            stored_id = trigger_comment_id(existing.get("trigger"))
            if stored_id is None:
                # A row written before triggers were recorded, or one whose
                # trigger does not parse, is handled: an old request must not
                # be retried just because the thread changed. Record the
                # comment so a later failure or head change cannot select it
                # again.
                if not _consume_mention(
                    store, row_id, existing, trigger, item, runner
                ):
                    skipped += 1
                continue
            if comment_id <= stored_id:
                # Comment ids grow monotonically, so a comment at or before
                # the recorded one was handled already. Deleting or editing a
                # later mention must not bring an earlier one back.
                _mark_thread_read(item, runner)
                continue
            state = existing.get("state")
            if state in ("queued", "running"):
                # The job is already in flight; record this comment before
                # marking the thread read so it is not replayed after a later
                # failure.
                if not _consume_mention(
                    store, row_id, existing, trigger, item, runner
                ):
                    skipped += 1
                continue
            if state not in ("failed", "done"):
                skipped += 1
                continue
        else:
            state = None

        # The head is only needed to create a row or to tell whether a done
        # pull request moved. Queued, running and failed rows are decided
        # without it.
        head_sha = (
            _head_sha(repo, ref, runner)
            if existing is None or state == "done"
            else None
        )

        if existing is None:
            row = job_row(
                session_id=session_id,
                repo=repo,
                ref=ref,
                job_type=job_type,
                actor=actor,
                queue_type=skill.queue_type,
                trigger=trigger,
                head_sha=head_sha,
            )
            store.write("job", "insert", row["id"], row)
            created.append(row["id"])
            _mark_thread_read(item, runner)
            continue

        if state == "failed":
            # The id is the lookup key (any other id was skipped above), and
            # both admitted states may move to `queued`, so `retry_payload`
            # cannot return None here.
            payload = retry_payload(existing)
            payload["trigger"] = trigger
            payload["actor"] = actor
            payload["job_type"] = job_type
            payload["queue_type"] = skill.queue_type
        else:
            # The state guard above admits only `failed` and `done` here.
            old_head = existing.get("head_sha")
            if (
                head_sha is None
                and isinstance(old_head, str)
                and _HEAD_SHA.fullmatch(old_head) is not None
            ):
                # The current head could not be read and the row already
                # records a commit, so the head is unknown rather than
                # unchanged. Consuming here would discard a real request after
                # a rate limit or a network error; leave the thread unread for
                # a pass that can read the head.
                skipped += 1
                continue
            if (
                head_sha is None
                or not isinstance(old_head, str)
                or old_head == head_sha
            ):
                # Nothing moved, or it cannot be shown that it did: the request
                # is a duplicate rather than a new generation. Record it before
                # marking the thread read so a head change later does not
                # replay this same comment.
                if not _consume_mention(
                    store, row_id, existing, trigger, item, runner
                ):
                    skipped += 1
                continue
            payload = retry_payload(existing)
            now = utcnow()
            created_at = existing.get("created_at")
            if isinstance(created_at, str) and now <= created_at:
                # Status activity ids and the edit marker are derived from
                # `created_at` + `attempts`, and utcnow() has second precision.
                # Re-queueing with the same created_at would reuse the previous
                # generation's identity. Leave the thread unread so the next
                # pass re-evaluates it once the clock has moved.
                skipped += 1
                continue
            payload.update(
                {
                    "created_at": now,
                    "updated_at": now,
                    "attempts": 0,
                    "head_sha": head_sha,
                    "trigger": trigger,
                    "actor": actor,
                    "job_type": job_type,
                    "queue_type": skill.queue_type,
                    "session_id": session_id,
                }
            )

        try:
            # The key the row was found under goes with it: the callback
            # refuses a row whose stored id differs, so the check that keeps a
            # re-queue off another job's files lives in one place.
            accepted = requeue(row_id, existing, payload)
        except Exception:
            # A re-queue that failed leaves the previous attempt's evidence
            # partly removed; the row is unchanged, so the next pass tries
            # again. Never mark the thread read on a failure.
            accepted = False
        if not accepted:
            skipped += 1
            continue
        _mark_thread_read(item, runner)
        requeued.append(row_id)

    return created, requeued, skipped


def _repo_is_private(repo: str, runner: Callable[[list[str]], Completed]) -> bool:
    """Whether the repository is private. Unreadable counts as private.

    Fail-closed on purpose: treating an unknown repository as public would send
    it down the path that skips the private allow-list.
    """
    completed = runner(["gh", "api", f"repos/{repo}", "--jq", ".private"])
    if completed.returncode != 0:
        return True
    answer = (completed.stdout or "").strip().lower()
    if answer == "false":
        return False
    if answer == "true":
        return True
    return True
