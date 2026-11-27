"""A double of the scale-set sidecar `agent-ci-listener` (standard library only): it speaks the sidecar's JSON lines
(`agent_runner.ci.bridge`) from a scenario file instead of a scale-set session.

    python -m agent_doubles.ci.listener <scenario.json>

Scenario: `{"scale_set_id": 1, "batches": [...], "give": [request ids] | null, "jit_fail": false, "noise": false,
"then": "exit" | "serve", "exit_code": 0, "record": "<path>" | null}`.

- `ready` first, then (with `noise`) a line that is no JSON object and one of an unknown type;
- for each batch: the `batch` line (`type` and a `message_id` are added), then the controller's lines are read until
  `acquire` names that `message_id`; `acquired` names the requested IDs that are in `give` (all when null);
- `jit {ref, name}` is answered `jit {ref, encoded: "jit-<name>"}` (`failed {ref, error: "github_error"}` with
  `jit_fail`), `remove {ref}` `removed {ref}`; `capacity` is only recorded;
- after the batches: `exit` ends at once with `exit_code`, `serve` answers until the controller closes stdin;
- `record` gets every line of the controller (JSON lines), for the test's assertions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO


def _say(out: TextIO, value: Any) -> None:
    out.write(json.dumps(value) + "\n")
    out.flush()


def _answer(out: TextIO, line: dict[str, Any], scenario: dict[str, Any]) -> None:
    if line.get("type") == "jit":
        if scenario.get("jit_fail"):
            _say(out, {"type": "failed", "ref": line.get("ref"), "error": "github_error"})
        else:
            _say(out, {"type": "jit", "ref": line.get("ref"), "encoded": f"jit-{line.get('name')}"})
    elif line.get("type") == "remove":
        _say(out, {"type": "removed", "ref": line.get("ref")})


def serve(scenario: dict[str, Any], source: TextIO, out: TextIO) -> int:
    record = Path(scenario["record"]) if scenario.get("record") else None

    def read() -> dict[str, Any] | None:
        raw = source.readline()
        if not raw:
            return None
        line = json.loads(raw)
        if record is not None:
            with record.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
        return line if isinstance(line, dict) else {}

    _say(out, {"type": "ready", "scale_set_id": scenario.get("scale_set_id", 1)})
    if scenario.get("noise"):
        out.write("not json\n")
        _say(out, {"type": "unexpected"})
    give = scenario.get("give")
    for index, batch in enumerate(scenario.get("batches", [])):
        message_id = batch.get("message_id", index)
        _say(out, {**batch, "type": "batch", "message_id": message_id})
        while (line := read()) is not None:
            if line.get("type") == "acquire" and line.get("message_id") == message_id:
                chosen = [ident for ident in line.get("request_ids", []) if give is None or ident in give]
                _say(out, {"type": "acquired", "message_id": message_id, "request_ids": chosen})
                break
            _answer(out, line, scenario)
        else:
            return int(scenario.get("exit_code", 0))
    if scenario.get("then") == "serve":
        while (line := read()) is not None:
            _answer(out, line, scenario)
    return int(scenario.get("exit_code", 0))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    scenario = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    return serve(scenario, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
