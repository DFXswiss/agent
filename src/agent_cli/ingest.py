"""Turn a GitHub mention into a runner job.

No network of its own: `gh` runs through the injected runner, the way `watch.py`
does it. Whether a request is admitted is decided in `jobs.py`; this module reads
notifications, works out what was asked for, and writes the row.

Every step is fail-closed. A notification that cannot be parsed, a body that
cannot be read, a policy that does not name the actor — each ends the request
rather than widening it. The cost of skipping one mention is that somebody asks
again; the cost of admitting one wrongly is a bot acting on a stranger's word.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .jobs import Verdict, admits, job_id
from .runtime import Completed
from .store import Store, utcnow

# A mention inside a markdown quote is a citation, not an address. Someone
# quoting an earlier request must not trigger a second run of it.
_QUOTE = re.compile(r"^\s*>.*$", re.MULTILINE)
# The same holds for code: a handle shown in a fenced block or an inline span is
# an example being displayed, not somebody being addressed. Documenting how to
# call this bot would otherwise call it.
_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_CODE_SPAN = re.compile(r"`[^`\n]*`")


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


def requested_job_type(body: Any, policy: Any, allowed: Any = None) -> str | None:
    """The job type a mention asks for, or the policy's default.

    An unknown type is not silently replaced by the default: asking for
    something this instance does not do is a different case from not asking.
    """
    default = None
    if isinstance(policy, dict):
        raw = policy.get("default_skill_on_mention")
        if isinstance(raw, str) and raw.strip():
            default = raw.strip().lower()
    if not isinstance(body, str):
        return default
    known = [t.lower() for t in allowed] if isinstance(allowed, list) else []
    # Same treatment as the mention itself: a type shown as an example is not a
    # type being asked for.
    text = _addressing_text(body)
    for candidate in known:
        if re.search(rf"(^|[^a-z0-9-]){re.escape(candidate)}([^a-z0-9-]|$)", text):
            return candidate
    return default


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


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


def job_row(
    *,
    session_id: str,
    repo: str,
    ref: str,
    job_type: str,
    actor: str,
) -> dict[str, Any]:
    """A fresh job row in `queued`."""
    now = utcnow()
    return {
        "id": job_id(repo, ref, job_type),
        "session_id": session_id,
        "repo": repo,
        "ref": ref,
        "job_type": job_type,
        "actor": actor,
        "state": "queued",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }


def scan_mentions(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    session_id: str,
    login: str,
    policy: Any,
) -> tuple[list[str], int]:
    """Insert a queued job for every notification this instance is admitted for.

    Returns the job ids created and the number of notifications skipped. A job
    that already exists is neither created nor counted as skipped: the same
    request arriving twice is one job, which is what the identity is for.
    """
    created: list[str] = []
    skipped = 0

    completed = runner(["gh", "api", "--paginate", "--slurp", "notifications"])
    if completed.returncode != 0:
        return created, 0
    try:
        pages = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return created, 0
    items = [i for page in pages if isinstance(page, list) for i in page]

    allowed_types = policy.get("job_types_allow") if isinstance(policy, dict) else None

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

        addressed = [c for c in comments if isinstance(c, dict) and mentions(c.get("body"), login)]
        if not addressed:
            skipped += 1
            continue
        last = addressed[-1]
        user = last.get("user")
        actor = user.get("login") if isinstance(user, dict) else None
        if not isinstance(actor, str) or not actor:
            skipped += 1
            continue

        job_type = requested_job_type(last.get("body"), policy, allowed_types)
        if not job_type:
            # Type-defensive rather than behaviour-changing: `admits` rejects a
            # missing job type on its own, so removing this line changes no
            # outcome. It exists to keep `None` out of a `str` parameter, and a
            # mutation of it is therefore green by construction.
            skipped += 1
            continue

        private = bool(_repo_is_private(repo, runner))
        verdict: Verdict = admits(
            policy, actor=actor, repo=repo, job_type=job_type, private=private
        )
        if not verdict.admitted:
            skipped += 1
            continue

        row = job_row(
            session_id=session_id, repo=repo, ref=ref, job_type=job_type, actor=actor
        )
        if store.row("job", row["id"]) is not None:
            continue
        store.write("job", "insert", row["id"], row)
        created.append(row["id"])

    return created, skipped


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
