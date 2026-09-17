"""Catch runner config that would only fail when the first job runs.

The existing runner's health command mostly probes its environment —
binaries on PATH, files readable, directories writable — which a pure
module cannot do and which belongs where the command is wired. Three of
its checks are pure config rules, and those are what this module
carries: each catches a misconfiguration that otherwise stays invisible
until the first real job fails.

Pure module: no subprocess, no network, no filesystem, no Store.
Callers supply the parsed catalogue and configs; this module only
applies the rules. Unusable input is reported as a problem rather than
silently read as healthy, with one documented exception: a catalogue entry
the original's own reader drops without complaint — a null, or a mapping
carrying no usable id — is dropped here too. `unresolved_skills` says
where and why, and records the narrow shapes where this port is stricter
than the original rather than laxer. None of these functions raise.
"""

from __future__ import annotations

from typing import Any


def unresolved_skills(catalog: Any, runner_config: Any) -> list[str]:
    """Skill ids whose deny-list or timeout does not actually resolve.

    The catalogue's `skills` may be a list or a mapping; a mapping is read
    by its values, as the original's iterator does. Collect each entry's
    `id` when the entry is a dict and `id` is a non-empty string.

    `timeout_minutes` is resolved for presence only — its type rule lives
    in `runner_config_problems`. `deny` is resolved and then required to be
    a list of strings, because the worker refuses the job otherwise.

    An empty list means every collected id resolved. Unusable input is
    reported as a problem rather than read as healthy, except for the two
    entry shapes the module docstring names: those are dropped, and the
    comments below say which and why.
    """
    raw_entries = catalog.get("skills") if isinstance(catalog, dict) else None
    if isinstance(raw_entries, list):
        entries = raw_entries
    elif isinstance(raw_entries, dict):
        # The original iterates with `jq '.skills[]?'`, which walks an
        # object's values as readily as an array's elements — measured: an
        # object-shaped catalogue yields every id at rc=0. Requiring a list
        # would condemn a catalogue the original reads without complaint.
        entries = list(raw_entries.values())
    else:
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
        if entry is not None and not isinstance(entry, dict):
            # The original reads the catalogue with `jq '.skills[]?.id'`,
            # where the `?` guards the iteration only. A scalar element makes
            # the field access raise, jq exits non-zero, and the whole
            # coverage check reports the catalogue as unreadable. Measured:
            # a bare string element gives rc=5 where an all-object catalogue
            # gives rc=0. Skipping such an entry instead would return [] —
            # the healthy answer — for a catalogue nobody can read.
            return ["the catalogue cannot be read"]
        if not isinstance(entry, dict):
            # `null` is the one non-dict the original tolerates, because jq
            # reads `null.id` as null and drops it rather than raising.
            continue
        raw_id = entry.get("id")
        # The original prints ids through `jq -r`, which stringifies a
        # number: `{"id": 7}` becomes the skill "7" and is checked. Measured
        # against Python's str() for both int and float, the two agree
        # exactly, so a numeric id is coerced here as well rather than
        # dropped. Skipping it would leave a skill unchecked that the
        # original checks, which is the direction this module must not fail
        # in. bool is excluded despite being an int subclass: jq renders it
        # "true" where str() gives "True", so coercing it would invent an id
        # the original never produces.
        if isinstance(raw_id, (int, float)) and not isinstance(raw_id, bool):
            skill_id = str(raw_id)
        else:
            skill_id = raw_id
        if not isinstance(skill_id, str) or not skill_id:
            # A dict with no usable `id` is dropped by the original too, via
            # `// empty`. A bool, list or mapping id stays dropped: jq does
            # render those, but a list or mapping comes out as multi-line
            # JSON that the original then splits into several garbage ids,
            # which is an artefact of reading ids line by line rather than a
            # rule worth reproducing.
            continue
        # Each path is defended on its own. A setting resolves from the
        # skill's own entry or from defaults, and either alone is enough —
        # a config carrying only defaults is ordinary, and gating on both
        # sub-structures existing would flag every skill in one.
        skill_cfg = skills_table.get(skill_id) if isinstance(skills_table, dict) else None
        if skill_cfg is not None and not isinstance(skill_cfg, dict):
            # The worker reads `.skills[<id>].deny` through jq, and indexing
            # a non-object raises: measured rc=5 where a proper mapping gives
            # rc=0, and the worker then refuses the job. Falling through to
            # defaults here would call that configuration clean.
            problems.append(f"skill {skill_id} has an unusable entry")
            continue
        skill_map = skill_cfg if isinstance(skill_cfg, dict) else {}
        defaults_map = defaults if isinstance(defaults, dict) else {}
        # A key present with value None is the same as a missing key:
        # the worker would still discover the gap at run time.
        #
        # `deny` is checked for usability, not merely presence. The worker
        # requires an array of strings and refuses the job when it is not
        # one, so a `deny` of `"Bash"` or `[1]` would satisfy a presence
        # check here and then fail the first real job — the failure this
        # module exists to prevent. `timeout_minutes` needs no equivalent
        # here because `runner_config_problems` carries its type rule.
        #
        # Two degenerate shapes are treated more strictly here than by the
        # worker, deliberately. The worker round-trips every value through
        # `jq -r` text: a `deny` written as the JSON string "null" prints as
        # bare `null` and is read as absent, and one written as the string
        # "[]" is re-parsed back into an empty array. Both are accepted
        # there and reported here. Emulating that would mean re-parsing
        # strings as JSON inside a config check, which is a bash text
        # artefact rather than a rule, and it would make this check accept
        # values that are genuinely the wrong type. Over-strict is the safe
        # direction: it flags a config the worker would have run, never
        # passes one the worker refuses.
        deny = skill_map.get("deny")
        if deny is None:
            deny = defaults_map.get("deny")
        if deny is None:
            problems.append(f"skill {skill_id} is missing deny")
        elif not isinstance(deny, list) or not all(isinstance(x, str) for x in deny):
            problems.append(f"skill {skill_id} has an unusable deny")
        if (
            skill_map.get("timeout_minutes") is None
            and defaults_map.get("timeout_minutes") is None
        ):
            problems.append(f"skill {skill_id} is missing timeout_minutes")
    return problems


def _positive_budget(value: Any) -> bool:
    """True for an int strictly greater than zero (bool rejected).

    Int, not "number". The original tests jq's `type == "number"`, which
    admits a float. The two budgets `_budget` resolves — `timeout_minutes`
    and `stall_minutes` — are usable only as ints, because `_budget`
    returns a value only for an int: a float resolves to None and the job
    is skipped. Accepting a float would mean this check passing a
    configuration the supervisor cannot run, the exact failure this module
    exists to prevent, so the laxer half of the original is deliberately
    not carried over.

    The one other field held to this rule, `clone_stall_minutes`, has no
    reader in the ported Python yet. It is checked the same way for
    consistency and because the clone path will read it, not because that
    argument already applies to it.
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

    Three budgets must be positive ints, a `skills` table must be a mapping
    if it is there at all, and every skill's effective timeout — its own, or
    the default it inherits — must be a positive int too.

    Per skill, the effective timeout is checked; a `stall_minutes` override
    is checked only when the skill actually carries one, because an absent
    one inherits the default that was already checked above.

    The point of the per-skill rules is that every value `_budget` could
    resolve at run time has been checked here. Whatever it resolves — a
    skill's own override or the inherited default — this has already
    required it to be a positive whole number, so a config that passes
    cannot then fail to produce a budget. Anything added to `_budget`'s
    reads later needs a rule here too, or that property quietly lapses.

    Two deliberate differences from the original. It reports this whole
    block as a single message; this returns one per broken rule, because a
    caller that has to print them is better served naming the field. And
    it requires *strictly* positive where `_budget` accepts zero at run
    time — the original is likewise stricter in its check than in its
    runtime for these fields. That direction is the safe one: it rejects a
    config the runtime would have tolerated, rather than passing one the
    runtime cannot use.

    `clone_stall_minutes` is the exception to the paragraph above: nothing
    in the ported Python reads it yet. It is checked because the original's
    configuration rule covers it and the clone path will read it once that
    is ported.
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
    if skills is None:
        # The skills table holds overrides, so a config that takes every
        # budget from defaults does not need to carry one. Absent and empty
        # take the same path through every consumer: `_budget` falls through
        # to the defaults from both, and `unresolved_skills` leaves the
        # catalogue skill to resolve from defaults in both. Whether it then
        # resolves depends on those defaults, not on which of the two shapes
        # the table had. Rejecting only the absent one would report a problem
        # in a configuration the supervisor demonstrably runs, which is the
        # opposite of this module's job. Deliberate divergence: the
        # original's rule requires the object to be there.
        skills = {}
    elif not isinstance(skills, dict):
        problems.append("skills is not a mapping")
        return problems
    default_timeout = defaults_map.get("timeout_minutes")
    for skill_id, skill_cfg in skills.items():
        own_timeout = (
            skill_cfg.get("timeout_minutes") if isinstance(skill_cfg, dict) else None
        )
        effective = default_timeout if own_timeout is None else own_timeout
        if not _positive_budget(effective):
            problems.append(f"skill {skill_id} has no positive whole-number timeout_minutes")
        # The original has no per-skill rule for stall_minutes, but the
        # supervisor reads a skill's own override for it exactly as it does
        # the timeout, and it substitutes the default only when the value is
        # absent — never when it is present and malformed. An override that
        # exists therefore has to be usable by itself; inheriting a healthy
        # default is not what would happen. Without this rule a skill
        # carrying `stall_minutes: 1.5` passes every check here and then has
        # each of its non-overdue jobs skipped for good.
        own_stall = (
            skill_cfg.get("stall_minutes") if isinstance(skill_cfg, dict) else None
        )
        if own_stall is not None and not _positive_budget(own_stall):
            problems.append(f"skill {skill_id} has no positive whole-number stall_minutes")
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
