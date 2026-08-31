"""Runner job lifecycle and admission.

No Postgres, no network, no subprocess. The store holds job rows; this module
decides what a job is called, which state may follow which, and whether a
request is admitted at all. The runner commands do the I/O.

Admission is fail-closed on purpose: an empty allow-list denies. A policy that
cannot be read, or that is missing a list, must never widen what this instance
answers to — a runner that starts taking work from everyone because a file went
missing is worse than one that stops.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Deliberately ordered: a job moves forward through these, and `failed` is not
# terminal because a fresh mention re-queues the same job id.
STATES = ("queued", "running", "done", "failed")

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "queued": ("running",),
    "running": ("done", "failed"),
    "done": ("queued",),
    "failed": ("queued",),
}

# The identity has to survive a retry: the same pull request reviewed again is
# the same job, not a second one. Anything outside this set would make a retry
# look like new work and defeat the de-duplication the queue depends on.
_ID_SAFE = re.compile(r"[^a-z0-9]+")


def job_id(repo: str, ref: str, job_type: str) -> str:
    """Stable identity for one unit of work: same request, same id."""
    parts = [repo, ref, job_type]
    if not all(isinstance(p, str) and p.strip() for p in parts):
        raise ValueError("job_id requires repo, ref and job_type")
    slug = "__".join(_ID_SAFE.sub("_", p.strip().lower()).strip("_") for p in parts)
    if not slug.strip("_"):
        raise ValueError("job_id produced an empty identity")
    return slug


def transition_allowed(old: str, new: str) -> bool:
    """Whether a job may move from `old` to `new`."""
    return new in TRANSITIONS.get(old, ())


@dataclass(frozen=True)
class Verdict:
    """Admitted or not, and the reason — the reason is what gets logged."""

    admitted: bool
    reason: str


def _listed(policy: Any, key: str) -> list[str]:
    """A policy list, lowercased. A missing or malformed list reads as empty."""
    if not isinstance(policy, dict):
        return []
    raw = policy.get(key)
    if not isinstance(raw, list):
        return []
    return [item.lower() for item in raw if isinstance(item, str) and item]


def admits(
    policy: Any,
    *,
    actor: str,
    repo: str,
    job_type: str,
    private: bool = False,
) -> Verdict:
    """Whether this instance answers to this request.

    Deny-lists win over allow-lists, comparison is case-insensitive, and every
    allow-list must name the value — an empty one admits nothing.
    """
    if not isinstance(policy, dict):
        return Verdict(False, "policy is not an object")
    if policy.get("enabled") is False:
        return Verdict(False, "instance disabled by policy")

    for label, value in (("actor", actor), ("repo", repo), ("job type", job_type)):
        if not isinstance(value, str) or not value.strip():
            return Verdict(False, f"{label} is missing")

    actor_l, repo_l, type_l = actor.lower(), repo.lower(), job_type.lower()

    if actor_l in _listed(policy, "actors_deny"):
        return Verdict(False, f"actor {actor} is denied")
    if repo_l in _listed(policy, "repos_deny"):
        return Verdict(False, f"repo {repo} is denied")

    if actor_l not in _listed(policy, "actors_allow"):
        return Verdict(False, f"actor {actor} is not allowed")
    if repo_l not in _listed(policy, "repos_allow"):
        return Verdict(False, f"repo {repo} is not allowed")
    if type_l not in _listed(policy, "job_types_allow"):
        return Verdict(False, f"job type {job_type} is not allowed")

    if private:
        # A private repository needs naming twice: once in repos_allow and again
        # here. Reaching into private code is the decision worth stating twice.
        identity = policy.get("agent_identity")
        exceptions = _listed(identity if isinstance(identity, dict) else {}, "private_repos_allow")
        if repo_l not in exceptions:
            return Verdict(False, f"private repo {repo} is not in private_repos_allow")
        return Verdict(True, f"admitted, private repo {repo} named in private_repos_allow")

    return Verdict(True, "admitted")
