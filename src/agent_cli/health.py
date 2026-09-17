"""Catch runner config that would only fail when the first job runs.

The existing runner's health command mostly probes its environment —
binaries on PATH, files readable, directories writable — which a pure
module cannot do and which belongs where the command is wired. Two of
its checks are pure config rules, and those are what this module
carries: both catch a misconfiguration that otherwise stays invisible
until the first real job fails.

Pure module: no subprocess, no network, no filesystem, no Store.
Callers supply the parsed catalogue and configs; this module only
applies the rules. Unusable input is reported as a problem, never
silently treated as healthy. Neither function raises.
"""

from __future__ import annotations

from typing import Any


def unresolved_skills(catalog: Any, runner_config: Any) -> list[str]:
    """Skill ids whose deny-list or timeout does not actually resolve.

    Collect each catalogue entry's `id` when the entry is a dict and
    `id` is a non-empty string. A setting is resolved when it is present
    (and not None) on that skill or in defaults. An empty list means
    every collected id resolved. Unusable input is reported as a
    problem, never as healthy.
    """
    entries = catalog.get("skills") if isinstance(catalog, dict) else None
    if not isinstance(entries, list):
        # Returning [] here would read as healthy to a caller, which is
        # the opposite of the truth: the catalogue cannot be read, so
        # whether any skill resolves is unknown.
        return ["the catalogue cannot be read"]

    skills_table = (
        runner_config.get("skills") if isinstance(runner_config, dict) else None
    )
    defaults = (
        runner_config.get("defaults") if isinstance(runner_config, dict) else None
    )

    problems: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        skill_id = entry.get("id")
        if not isinstance(skill_id, str) or not skill_id:
            continue
        # Each path is defended on its own. A setting resolves from the
        # skill's own entry or from defaults, and either alone is enough —
        # a config carrying only defaults is ordinary, and gating on both
        # sub-structures existing would flag every skill in one.
        skill_cfg = skills_table.get(skill_id) if isinstance(skills_table, dict) else None
        skill_map = skill_cfg if isinstance(skill_cfg, dict) else {}
        defaults_map = defaults if isinstance(defaults, dict) else {}
        # A key present with value None is the same as a missing key:
        # the worker would still discover the gap at run time.
        if skill_map.get("deny") is None and defaults_map.get("deny") is None:
            problems.append(f"skill {skill_id} is missing deny")
        if (
            skill_map.get("timeout_minutes") is None
            and defaults_map.get("timeout_minutes") is None
        ):
            problems.append(f"skill {skill_id} is missing timeout_minutes")
    return problems


def agent_config_problems(agent_config: Any) -> list[str]:
    """Problems in a runner's agent config, at most one per pin.

    A runner pins cli='agent', session_kind='runner', and exactly the
    three skills spine, review-loop, pr-review. An empty list means the
    pins hold. Unusable input is reported as a problem, never as healthy.
    """
    if not isinstance(agent_config, dict):
        return ["agent config is not a dict"]
    problems: list[str] = []
    if agent_config.get("cli") != "agent":
        problems.append("cli is not 'agent'")
    if agent_config.get("session_kind") != "runner":
        problems.append("session_kind is not 'runner'")
    skills = agent_config.get("skills")
    # Order-agnostic, matching what this ports: all three present, nothing
    # else, no duplicates. A config listing the same three in a different
    # order pins the same shape and must not be called a problem.
    if (
        not isinstance(skills, list)
        or len(skills) != 3
        or set(skills) != {"spine", "review-loop", "pr-review"}
    ):
        problems.append(
            "skills is not exactly spine, review-loop and pr-review"
        )
    return problems
