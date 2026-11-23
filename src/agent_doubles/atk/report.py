"""Result table of the attack suite (``docs/platform/atk/M0.md`` and the run's ``results.json``).

``outcome(attempt)`` names how Claude Code answered one attempt, from the tool result it sent back to the model:

| Outcome | Tool result |
|---|---|
| ``absent`` | ``No such tool available`` (the tool is not offered: ``--tools`` / deny list) |
| ``hook-deny`` | ``PreToolUse:<tool> hook error: …`` (the guest policy denied it) |
| ``denied`` | any other error (managed ``permissions.deny``, ``--restricted`` confinement, OS refusal) |
| ``allowed`` | no error (a harmless action inside the workspace, e.g. writing ``evil.sh`` as a plain file) |
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from agent_doubles.atk import catalog


def outcome(attempt: Mapping[str, Any]) -> str:
    result = str(attempt.get("result", ""))
    if attempt.get("is_error") is not True:
        return "allowed"
    if "No such tool available" in result:
        return "absent"
    if "hook error" in result:
        return "hook-deny"
    return "denied"


def cell(verdict: Mapping[str, Any] | None) -> str:
    if verdict is None:
        return "not run"
    attempts = verdict.get("attempts", [])
    shown = ", ".join(f"{a['tool']}: {outcome(a)}" for a in attempts) or "no attempt"
    return ("pass" if verdict.get("passed") else "**FAIL**") + " - " + shown


def evidence_cell(verdicts: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for verdict in verdicts:
        criteria = verdict.get("criteria", {})
        sources = verdict.get("journal_sources", {})
        egress = criteria.get("no_egress", {}).get("details", [""])
        specific = criteria.get("specific")
        shown = (
            "" if specific is None else "; specific checks ok" if specific.get("ok") else "; **specific checks FAIL**"
        )
        parts.append(
            f"{verdict.get('mode')}: journal {sources.get('hook', 0)} hook / {sources.get('transcript', 0)} transcript;"
            f" {egress[0] if egress else ''}{shown}"
        )
    return "<br>".join(parts)


def table(results: Sequence[Mapping[str, Any]]) -> str:
    """Markdown table: one row per attack, one column per mode, plus the evidence summary."""
    by_key = {(str(item["attack"]), str(item["mode"])): item for item in results}
    lines = [
        "| ID | Group | Attack | " + " | ".join(catalog.MODE_TITLES[mode] for mode in catalog.MODES) + " | Evidence |",
        "|---|---|---|" + "---|" * len(catalog.MODES) + "---|",
    ]
    for attack in catalog.ATTACKS:
        verdicts = [by_key.get((attack.id, mode)) for mode in catalog.MODES]
        cells = " | ".join(cell(verdict) for verdict in verdicts)
        found = [verdict for verdict in verdicts if verdict is not None]
        lines.append(f"| {attack.id} | {attack.group} | {attack.title} | {cells} | {evidence_cell(found)} |")
    return "\n".join(lines) + "\n"


def probe_table(probes: Mapping[str, Mapping[str, Any]]) -> str:
    """Markdown table of the sandbox self-test per mode (checks and outcomes)."""
    modes = [mode for mode in catalog.MODES if mode in probes]
    checks: dict[str, None] = {}
    for mode in modes:
        for check in probes[mode].get("checks", []):
            checks.setdefault(str(check["check"]), None)
    lines = ["| Check | " + " | ".join(modes) + " |", "|---|" + "---|" * len(modes)]
    for name in checks:
        row = []
        for mode in modes:
            found = next((c for c in probes[mode].get("checks", []) if c["check"] == name), None)
            row.append("-" if found is None else f"{'ok' if found['ok'] else '**FAIL**'} `{found['outcome']}`")
        lines.append(f"| {name} | " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def os_table(probes: Mapping[str, Mapping[str, Any]]) -> str:
    """Markdown table: the OS part of the shell attacks (``catalog.OS_CHECKS``) with the self-test outcomes."""
    modes = [mode for mode in catalog.MODES if mode in probes]
    lines = ["| ID | Self-test checks | " + " | ".join(modes) + " |", "|---|---|" + "---|" * len(modes)]
    for attack_id, names in catalog.OS_CHECKS.items():
        row = []
        for mode in modes:
            found = {str(c["check"]): c for c in probes[mode].get("checks", [])}
            row.append(
                ", ".join(
                    "missing" if name not in found else ("ok" if found[name]["ok"] else "**FAIL**") for name in names
                )
            )
        lines.append(f"| {attack_id} | {', '.join(f'`{name}`' for name in names)} | " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"
