"""Working modes of a task (KOR-75, ADR-098 sections 1 to 3; the tool's side of the web's task form field
"Arbeitsweisen" and the mode chips with "Ausschalten" on the task page):

- `modes list`: the catalogue this hub offers (`GET /v1/modes`), every entry and one line per mode
  (`name  title  needs: …`);
- `task mode on --task --mode NAME [--mode NAME …]`: switch modes on (`PUT /v1/tasks/{task}/modes/{mode}` with `{}`,
  answer `{mode: task_mode}`), one after the other in the order given, so a mode and the modes it needs are chosen in
  one step (the needed ones first);
- `task mode off --task --mode NAME [--mode NAME …]`: switch them off (`DELETE /v1/tasks/{task}/modes/{mode}`).

`task create --mode` (`tasks.task_create`, field `modes` of `POST /v1/tasks`) chooses modes with the task, and
`task show` lists the task's modes (`view`). The agent acts for its person (way `tool`); nothing here switches a mode
on by itself. Catalogue v1 has no settings, so there is no `--mode-setting` yet.

The hub's refusals become clear next steps (`refusal`): `mode_needs_instruction` (an agent inside a platform session
switches a mode on only with the operation `task.mode` and a quote of the instruction), `mode_needs` (with `missing`,
the modes the refusal's `detail.modes` names as off) and, for `task mode`, `not_found` with the reason `mode_or_task`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agent_platform.account import Account
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import uuid_argument
from agent_platform.hub import segment

NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_MODES = 64
MODE_REFUSALS = ("mode_needs_instruction", "mode_needs")


def mode_name(text: str) -> str:
    """A name of the catalogue's form (`common.json#/$defs/mode_name`)."""
    if NAME_RE.fullmatch(text) is None:
        raise PlatformError("invalid_argument", reason="mode_invalid")
    return text


def mode_names(values: list[str]) -> list[str]:
    """The names in their order, each once (at most `MAX_MODES`)."""
    names = list(dict.fromkeys(mode_name(value) for value in values))
    if len(names) > MAX_MODES:
        raise PlatformError("invalid_argument", reason="mode_invalid")
    return names


def _missing(detail: Mapping[str, object]) -> list[str]:
    """The modes a `mode_needs` refusal names as off (`detail.modes`), empty when it names none."""
    value = detail.get("modes")
    if isinstance(value, list) and all(isinstance(name, str) and NAME_RE.fullmatch(name) for name in value):
        return [str(name) for name in value]
    return []


def refusal(error: PlatformError, *, unknown: bool = False) -> PlatformError:
    """The error to show for a hub refusal of a mode change (module docstring); any other error unchanged. `unknown`:
    a `not_found` means the task or the mode is not known."""
    code = error.fields.get("hub_error")
    if code in MODE_REFUSALS:
        extra = {"missing": _missing(error.detail)} if code == "mode_needs" else {}
        return PlatformError("hub_refused", **{**error.fields, "reason": code, **extra})
    if unknown and code == "not_found":
        return PlatformError("not_found", hub_error=code, reason="mode_or_task")
    return error


def _path(task: str, name: str) -> str:
    return f"/v1/tasks/{segment(task)}/modes/{segment(name)}"


def _needs(row: Mapping[str, Any]) -> list[str]:
    needs = row.get("needs")
    found: list[str] = []
    for key in ("modes", "prerequisites"):
        value = needs.get(key) if isinstance(needs, dict) else None
        found += [str(item) for item in value] if isinstance(value, list) else []
    return found


def mode_line(row: Mapping[str, Any]) -> str:
    """`name  title  needs: …` of one catalogue entry (the English title; the name when it has none)."""
    title = row.get("title")
    text = title.get("en") if isinstance(title, dict) else None
    needs = _needs(row)
    shown = text if isinstance(text, str) and text else row["name"]
    return f"{row['name']}  {shown}  needs: {', '.join(needs) if needs else 'nothing'}"


def modes_list(account: Account) -> dict[str, Any]:
    """The catalogue of the hub, in its order."""
    rows = account.call("GET", "/v1/modes").get("modes")
    if not isinstance(rows, list):
        raise PlatformError("hub_refused", reason="response_invalid")
    modes = [row for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)]
    return {"modes": modes, "lines": [mode_line(row) for row in modes]}


def modes_on(account: Account, task: str, names: list[str]) -> dict[str, Any]:
    """Switches the modes on in the order given; a refusal names the ones already on (`switched_on`)."""
    task = uuid_argument(task)
    switched: list[dict[str, Any]] = []
    for name in mode_names(names):
        try:
            answer = account.call("PUT", _path(task, name), {})
        except PlatformError as error:
            failed = refusal(error, unknown=True)
            if switched:
                failed.fields["switched_on"] = [row["mode"] for row in switched]
            raise failed from None
        row = answer.get("mode")
        if not isinstance(row, dict) or row.get("mode") != name:
            raise PlatformError("hub_refused", reason="response_invalid")
        switched.append(row)
    return {"task_id": task, "modes": switched}


def modes_off(account: Account, task: str, names: list[str]) -> dict[str, Any]:
    """Switches the modes off (at once for new steps, ADR-098 section 3)."""
    task = uuid_argument(task)
    wanted = mode_names(names)
    for name in wanted:
        try:
            account.call("DELETE", _path(task, name))
        except PlatformError as error:
            raise refusal(error, unknown=True) from None
    return {"task_id": task, "off": wanted}


def view(account: Account, org: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """The task's modes for `task show`: the rows of `task_row.modes` with the chooser's name, and the line
    `Modes: test (web, chosen by <name or id>)` (`Modes: none` without one, also from an older hub)."""
    listed = row.get("modes")
    modes = [mode for mode in listed if isinstance(mode, dict)] if isinstance(listed, list) else []
    people = account.names(org).members if modes else {}
    shown = [{**mode, "chosen_by_name": people.get(str(mode.get("chosen_by")))} for mode in modes]
    parts = [
        f"{mode.get('mode')} ({mode.get('way')}, chosen by {mode['chosen_by_name'] or mode.get('chosen_by')})"
        for mode in shown
    ]
    return {"modes": shown, "modes_line": f"Modes: {', '.join(parts) if parts else 'none'}"}


# --- commands ---


def _list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return modes_list(account)


def _on(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return modes_on(account, args.task, args.mode)


def _off(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return modes_off(account, args.task, args.mode)


def register(tree: Tree) -> None:
    group = tree.group("modes", "working modes of tasks (ADR-098)")
    tree.command(
        group,
        "list",
        _list,
        "the working modes this platform offers, one line per mode with what it needs (listing chooses nothing)",
        example="agent-platform modes list",
        words="arbeitsweisen katalog arbeitsweise modus verfügbar working modes catalogue available ways",
        then="agent-platform task mode on --task <task_id> --mode <name>",
    )
    task_group = tree.group("task", "tasks")
    mode_group = tree.group("mode", "working modes of a task (ADR-098)", task_group)
    for name, work, summary, example, words in (
        (
            "on",
            _on,
            "switch working modes on for a task (several --mode: in this order, the modes a mode needs first)",
            "agent-platform task mode on --task <task_id> --mode <name>",
            "arbeitsweise einschalten wählen aktivieren modus switch enable choose activate mode",
        ),
        (
            "off",
            _off,
            "switch working modes of a task off (at once for new steps)",
            "agent-platform task mode off --task <task_id> --mode <name>",
            "arbeitsweise ausschalten abschalten deaktivieren modus disable deactivate stop mode",
        ),
    ):
        sub = tree.command(mode_group, name, work, summary, writes=True, example=example, words=words)
        sub.add_argument("--task", required=True)
        sub.add_argument("--mode", required=True, action="append", help="a mode's name (`modes list`), repeatable")
