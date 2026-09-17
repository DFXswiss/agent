"""Catch runner config that would only fail when the first job runs.

The existing runner's health command mostly probes its environment —
binaries on PATH, files readable, directories writable — which a pure
module cannot do and which belongs where the command is wired. Three of
its checks are pure config rules, and those are what this module
carries: each catches a misconfiguration that otherwise stays invisible
until the first real job fails.

Pure module: no subprocess, no network, no filesystem, no Store.
Callers supply the parsed catalogue and configs; this module only
applies the rules. Unusable input is reported as a problem, never
silently treated as healthy. None of these functions raise.
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


def _positive_budget(value: Any) -> bool:
    """True for an int strictly greater than zero (bool rejected).

    Int, not "number". The original tests jq's `type == "number"`, which
    admits a float, but nothing downstream here can use one: `_budget`
    returns a budget only when it is an `int`, so a float setting resolves
    to None and the job is skipped. Accepting a float would mean this
    check passes a configuration the supervisor cannot run — the exact
    failure this module exists to prevent — so the laxer half of the
    original is deliberately not carried over.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value > 0


def runner_config_problems(runner_config: Any) -> list[str]:
    """Problems in the runner configuration's own budget settings.

    The per-skill check above covers `deny` and `timeout_minutes`, which is
    all the original checks per skill. `stall_minutes` is covered here
    instead, as a property of the configuration as a whole — the original
    draws the same line, and without this half nothing checks it at all.
    A config with no `stall_minutes` then passes the skill check and has
    every non-overdue running job skipped forever; an overdue one is still
    killed, because the timeout watchdog no longer depends on this budget.

    Three budgets must be positive ints, `skills` must be a mapping, and
    every skill's effective timeout — its own, or the default it inherits —
    must be a positive int too.

    Two deliberate differences from the original. It reports this whole
    block as a single message; this returns one per broken rule, because a
    caller that has to print them is better served naming the field. And
    it requires *strictly* positive where `_budget` accepts zero at run
    time: the original is stricter in its check than in its runtime for
    these same fields, and that asymmetry is harmless — it rejects a
    config the runtime would have tolerated, never the reverse.
    """
    if not isinstance(runner_config, dict):
        return ["runner config is not a dict"]
    problems: list[str] = []
    defaults = runner_config.get("defaults")
    defaults_map = defaults if isinstance(defaults, dict) else {}
    for field in ("timeout_minutes", "stall_minutes"):
        if not _positive_budget(defaults_map.get(field)):
            problems.append(f"defaults.{field} is not a positive whole number")
    if not _positive_budget(runner_config.get("clone_stall_minutes")):
        problems.append("clone_stall_minutes is not a positive whole number")
    skills = runner_config.get("skills")
    if not isinstance(skills, dict):
        problems.append("skills is not a mapping")
        return problems
    default_timeout = defaults_map.get("timeout_minutes")
    for skill_id, skill_cfg in skills.items():
        own = skill_cfg.get("timeout_minutes") if isinstance(skill_cfg, dict) else None
        effective = default_timeout if own is None else own
        if not _positive_budget(effective):
            problems.append(f"skill {skill_id} has no positive whole-number timeout_minutes")
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
        # Every element must be a string before the set comparison: an
        # unhashable element would make set() raise, and this function
        # reports unusable input as a problem rather than raising.
        or any(not isinstance(s, str) for s in skills)
        or set(skills) != {"spine", "review-loop", "pr-review"}
    ):
        problems.append(
            "skills is not exactly spine, review-loop and pr-review"
        )
    return problems
