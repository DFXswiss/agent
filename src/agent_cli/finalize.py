"""Decide how a finished job is recorded and whether the claim is credible.

An agent exiting 0 and writing DONE is a self-report. This module looks for an
independent artefact that the work happened and downgrades the recorded outcome
when that artefact is absent.
"""

from __future__ import annotations

import re
from typing import Any

DONE_KINDS: tuple[str, ...] = ("pr-reviewed", "pr-mergeable", "marker")


def done_kind(policy: Any, job_type: str) -> str:
    """Return the done_kind configured for job_type, or a derived default."""
    if isinstance(policy, dict):
        skills = policy.get("skills")
        if isinstance(skills, dict):
            skill = skills.get(job_type)
            if isinstance(skill, dict):
                raw = skill.get("done_kind")
                if isinstance(raw, str) and raw:
                    if raw in DONE_KINDS:
                        return raw
                    # Unrecognised setting must degrade to the weakest proof,
                    # never to a convenient one.
                    return "marker"
    # Derive from job type when the policy is silent or unusable.
    if job_type in ("pr-review", "pr-ready"):
        return "pr-reviewed"
    if job_type == "merge-conflict":
        return "pr-mergeable"
    return "marker"


def transcript_has_marker(transcript: str, ref: str) -> bool:
    """Return True if a line starting with DONE names ref from field 3 onward."""
    if not isinstance(transcript, str) or not isinstance(ref, str):
        return False
    # Strip a single leading # so callers may pass "#123" or "123".
    needle = ref[1:] if ref.startswith("#") else ref
    if not needle:
        return False
    for line in transcript.splitlines():
        # Only lines that start with DONE followed by whitespace qualify.
        # Across 61 real transcripts, 57 mention DONE somewhere but only
        # 32 at line start — without the anchor a line like
        # "- None of the lines `DONE review PR 206 …` — gate A did not pass"
        # would be read as success.
        if not line.startswith("DONE\t") and not line.startswith("DONE "):
            continue
        # Split on runs of spaces and tabs; the marker must appear from
        # the third field onward — field 1 is DONE, field 2 is the identifier,
        # so DONE <ref> alone is not a marker. Compare as strings so 01 does
        # not match a ref of 1.
        parts = re.split(r"[ \t]+", line)
        if len(parts) < 3:
            continue
        for part in parts[2:]:
            if part == needle:
                return True
    return False


def judge(
    *,
    outcome: str,
    kind: str,
    kinds: set[str] | None,
    contract_followed: bool,
    mergeable: bool | None = None,
) -> tuple[str, str] | None:
    """Return (final outcome, work_performed) or None if the check is pending."""
    if outcome != "success":
        return (outcome, "n_a")
    if kinds is None:
        # The output check could not be answered; retry rather than record
        # a half-formed conclusion.
        return None
    # The outcome downgrade and work_performed are computed independently:
    # a merge-conflict job that resolves the conflict and pushes without
    # commenting yields ("agent_failed", "yes") — work done by pushing a
    # commit without commenting.
    result = "agent_failed" if not kinds else outcome
    if kind == "pr-reviewed":
        work = "yes" if "review" in kinds else "no"
        return (result, work)
    if kind == "pr-mergeable":
        # Weaker than pr-reviewed: checks the pull request's current state,
        # not authorship, so it can hold without this job having caused it.
        if mergeable is True:
            return (result, "yes")
        if mergeable is False:
            return (result, "no")
        return (result, "unknown")
    if kind == "marker":
        # Weakest evidence, a self-report, recorded as such so the weakness
        # stays visible in the data rather than hidden.
        return (result, "yes" if contract_followed else "no")
    return (result, "unknown")
