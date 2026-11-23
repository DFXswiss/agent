"""Control routes of DR-01 (E2E-33): the export/restore cycle of the hub and the runner store wipe, against a scripted
node."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.context import Context
from orchestrator.routes import backup
from orchestrator.services import Cluster, Hub, Runner
from test_routes_guest import Result, ScriptNode, call, failed, make_ctx, ok

PRINT = {"orgs": [{"id": "o1", "seq": 3, "hash": "h"}], "audit": {"seq": 9, "hash": "a"}}
REBUILD = {"ok": True, "check": True, "rebuilt": {"orgs": 2}, "differences": []}
VERIFY = {"ok": True, "head": {"seq": 9, "hash": "a"}, "checkpoint": None}


class HubNode(ScriptNode):
    """A scripted node that also answers the hub's health check (and can refuse to keep the hub alive)."""

    def __init__(self, respond: Any, *, hub_starts: bool = True) -> None:
        super().__init__(respond)
        self.hub_starts = hub_starts

    def call(self, op: str, /, **args: Any) -> Any:
        if op == "http":
            return {"status": 200, "body": ""}
        if op == "spawn" and args["name"] == "hub" and not self.hub_starts:
            return 1
        if op == "log_tail" and args["name"] == "hub":
            return "hub log"
        return super().call(op, **args)


DEFAULTS: dict[str, Result] = {
    "fingerprint": ok(json.dumps(PRINT) + "\n"),
    "orgs": ok('["o1","o2"]\n'),
    "psql": ok(),
    "before_exists": ok("0\n"),
    "grep": {"rc": 1, "stdout": "", "stderr": ""},
    "mkdir": ok(),
    "rm": ok(),
    "migrate": ok("applied 6\n"),
    "export": ok("exported 5\n"),
    "restore": ok("restored 9\n"),
    "rebuild-projections": ok(json.dumps(REBUILD) + "\n"),
    "audit": ok("note\n" + json.dumps(VERIFY) + "\n"),
}


def _kind(argv: list[str]) -> str:
    if argv[0] == "/pg/psql":
        statement = argv[-1]
        if "pg_database" in statement:
            return "before_exists"
        return "fingerprint" if "json_build_object" in statement else "orgs" if "json_agg(id" in statement else "psql"
    if argv[0] in {"mkdir", "rm", "grep"}:
        return argv[0]
    if argv[:4] == ["/hub/python", "-m", "agent_hub.db", "migrate"]:
        return "migrate"
    assert argv[:3] == ["/hub/python", "-m", "agent_hub"], argv
    return argv[3]


def hub_respond(overrides: dict[str, Result] | None = None) -> Any:
    sql: list[str] = []
    found = {**DEFAULTS, **(overrides or {})}

    def respond(argv: list[str]) -> Result:
        if argv[0] == "/pg/psql":
            sql.append(argv[-1])
            if _kind(argv) in {"fingerprint", "orgs"}:
                assert argv[argv.index("-d") + 1] == "agent_hub", argv
        return found[_kind(argv)]

    respond.sql = sql  # type: ignore[attr-defined]
    return respond


def hub_ctx(tmp_path: Path, respond: Any, *, hub_starts: bool = True) -> tuple[Context, HubNode]:
    node = HubNode(respond, hub_starts=hub_starts)
    ctx = make_ctx(tmp_path, node)
    ctx.cluster = Cluster(node, "/pg", str(tmp_path / "pg"), 21005, {"PATH": "/usr/bin"}, password="pw")
    env = {"AGENT_HUB_DATABASE": "postgresql://hub_writer:w@127.0.0.1:21005/agent_hub", "AGENT_HUB_X": "1"}
    ctx.hub = Hub(node, "/hub/python", env, "127.0.0.1", 21001, sleep=lambda _s: None)
    node.alive_names.add("hub")
    return ctx, node


def test_export_restore_runs_the_whole_cycle(tmp_path: Path) -> None:
    respond = hub_respond()
    ctx, node = hub_ctx(tmp_path, respond)
    status, answer = call(ctx, "POST", "/hub/export-restore", {})
    assert status == 200
    assert answer == {
        "orgs": ["o1", "o2"],
        "exported": {"o1": "exported 5", "o2": "exported 5"},
        "plaintext": [],
        "migrated": "applied 6",
        "restored": "restored 9",
        "before": PRINT,
        "after": PRINT,
        "rebuild": REBUILD,
        "audit": VERIFY,
    }
    assert "hub" in node.alive_names  # started again
    backup_dir = f"{tmp_path}/backup"
    exports = [argv for argv in node.runs if argv[3:4] == ["export"]]
    assert exports == [
        ["/hub/python", "-m", "agent_hub", "export", "--org", org, "--out", f"{backup_dir}/{org}.jsonl"]
        for org in ("o1", "o2")
    ]
    restore = next(argv for argv in node.runs if argv[3:4] == ["restore"])
    assert restore[4:] == ["--in", f"{backup_dir}/o1.jsonl", "--in", f"{backup_dir}/o2.jsonl"]
    statements = respond.sql
    assert "ALTER DATABASE agent_hub RENAME TO agent_hub_before" in statements
    assert "CREATE DATABASE agent_hub OWNER hub_owner" in statements
    assert any(text.startswith("ALTER ROLE hub_owner PASSWORD '") for text in statements)
    assert "GRANT CONNECT ON DATABASE agent_hub TO e2e_reader" in statements
    assert statements[-1] == "DROP DATABASE IF EXISTS agent_hub_before WITH (FORCE)"
    assert statements.index("ALTER DATABASE agent_hub RENAME TO agent_hub_before") > statements.index(
        next(text for text in statements if "json_agg(id" in text)
    )


def test_the_export_files_are_scanned_for_canaries(tmp_path: Path) -> None:
    hits = ok("kz-b\nkz-a\nkz-b\n")
    ctx, node = hub_ctx(tmp_path, hub_respond({"grep": hits}))
    status, answer = call(ctx, "POST", "/hub/export-restore", {"canaries": ["kz-a", "kz-b", "kz-c"]})
    assert (status, answer["plaintext"]) == (200, ["kz-a", "kz-b"])
    scan = next(argv for argv in node.runs if argv[0] == "grep")
    backup_dir = f"{tmp_path}/backup"
    assert scan == [
        "grep", "-F", "-o", "-h", "-e", "kz-a", "-e", "kz-b", "-e", "kz-c", "--",
        f"{backup_dir}/o1.jsonl", f"{backup_dir}/o2.jsonl",
    ]  # fmt: skip
    ctx, _ = hub_ctx(tmp_path, hub_respond({"grep": failed(2, "grep: unreadable")}))
    status, answer = call(ctx, "POST", "/hub/export-restore", {"canaries": ["kz-a"]})
    assert (status, answer["detail"]) == (500, "scan: grep: unreadable")
    for bad in ({"canaries": "kz"}, {"canaries": [""]}, {"canaries": [1]}):
        assert call(ctx, "POST", "/hub/export-restore", bad)[0] == 400


def test_no_orgs_need_no_scan(tmp_path: Path) -> None:
    respond = hub_respond()

    def no_orgs(argv: list[str]) -> Result:
        return ok("[]\n") if _kind(argv) == "orgs" else respond(argv)

    ctx, node = hub_ctx(tmp_path, no_orgs)
    status, answer = call(ctx, "POST", "/hub/export-restore", {"canaries": ["kz-a"]})
    assert (status, answer["orgs"], answer["plaintext"]) == (200, [], [])
    assert not any(argv[0] == "grep" for argv in node.runs)


def test_an_unfinished_earlier_recovery_is_never_dropped(tmp_path: Path) -> None:
    respond = hub_respond({"before_exists": ok("1\n")})
    ctx, node = hub_ctx(tmp_path, respond)
    status, answer = call(ctx, "POST", "/hub/export-restore", {})
    assert (status, answer["error"]) == (409, "invalid")
    assert "hub" in node.alive_names  # not stopped
    assert not any("DROP" in text or "RENAME" in text for text in respond.sql)


def test_the_restore_runs_with_the_new_owner_url(tmp_path: Path) -> None:
    seen: dict[str, dict[str, str]] = {}
    respond = hub_respond()

    class Recording(HubNode):
        def call(self, op: str, /, **args: Any) -> Any:
            if op == "run" and args["argv"][3:4] in (["restore"], ["migrate"]):
                seen[args["argv"][3]] = dict(args["env"])
            return super().call(op, **args)

    node = Recording(respond)
    ctx = make_ctx(tmp_path, node)
    ctx.cluster = Cluster(node, "/pg", str(tmp_path / "pg"), 21005, {"PATH": "/usr/bin"}, password="pw")
    ctx.hub = Hub(node, "/hub/python", {"AGENT_HUB_DATABASE": "w"}, "127.0.0.1", 21001, sleep=lambda _s: None)
    assert call(ctx, "POST", "/hub/export-restore", {})[0] == 200
    password = next(text for text in respond.sql if text.startswith("ALTER ROLE")).split("'")[1]
    assert len(password) == 64
    owner = f"postgresql://hub_owner:{password}@127.0.0.1:21005/agent_hub?sslmode=disable"
    assert seen["restore"]["AGENT_HUB_OWNER_DATABASE"] == owner
    assert seen["migrate"]["AGENT_HUB_OWNER_DATABASE"] == owner


def test_export_restore_failures(tmp_path: Path) -> None:
    cases: list[tuple[dict[str, Result], str]] = [
        ({"fingerprint": ok("not json\n")}, "fingerprint: no JSON answer"),
        ({"mkdir": failed(1, "")}, "export: the backup directory cannot be created"),
        ({"export": failed(1, "failed org\n")}, "export: failed org"),
        ({"export": {"rc": 3, "stdout": "", "stderr": ""}}, "export: "),
        ({"migrate": failed(1, "failed owner")}, "migrate: agent-hub migrate failed: failed owner"),
        ({"restore": failed(1, "failed verify:audit_chain\n")}, "restore: failed verify:audit_chain"),
        ({"rebuild-projections": {"rc": 1, "stdout": '{"ok":false}\n', "stderr": ""}}, 'rebuild: {"ok":false}'),
        ({"audit": failed(1, "agent-hub: the audit chain does not verify")}, "audit: agent-hub: the audit chain"),
    ]
    for overrides, detail in cases:
        ctx, _ = hub_ctx(tmp_path, hub_respond(overrides))
        status, answer = call(ctx, "POST", "/hub/export-restore", {})
        assert (status, answer["detail"].startswith(detail)) == (500, True), (overrides, answer)
    ctx, _ = hub_ctx(tmp_path, hub_respond(), hub_starts=False)
    status, answer = call(ctx, "POST", "/hub/export-restore", {})
    assert (status, answer["detail"].startswith("start: hub exited")) == (500, True)
    ctx.cluster = None
    assert call(ctx, "POST", "/hub/export-restore", {})[0] == 409


# -- runner store wipe -------------------------------------------------------------------------------------------------


DEVICE = "01a20491-a8c7-7645-8beb-c2984e623d82"


def wipe_respond(overrides: dict[str, Result] | None = None) -> Any:
    found = {
        "sh": ok(json.dumps({"device_id": DEVICE, "kind": "platform"})),
        "psql": ok("7\n"),
        "rm": ok(),
        "adopt": ok(),
        **(overrides or {}),
    }

    def respond(argv: list[str]) -> Result:
        if argv[0] == "/bin/sh":
            return found["sh"]
        if argv[0] == "/pg/psql":
            return found["psql"]
        if argv[0] == "rm":
            return found["rm"]
        assert argv[3:5] == ["image", "adopt"], argv
        return found["adopt"]

    return respond


def wipe_ctx(tmp_path: Path, respond: Any) -> tuple[Context, ScriptNode, Runner]:
    node = ScriptNode(respond)
    ctx = make_ctx(tmp_path, node)
    ctx.cluster = Cluster(node, "/pg", str(tmp_path / "pg"), 21005, {"PATH": "/usr/bin"}, password="pw")
    runner = Runner(node, "runner-1", "/agent/python", {"AGENT_RUNNER_HOME": "/home/u/runner-1"})
    ctx.runners["runner-1"] = runner
    node.alive_names.add("runner-1")
    return ctx, node, runner


def test_wipe_removes_the_store_keeps_the_identity_and_restarts(tmp_path: Path) -> None:
    ctx, node, _ = wipe_ctx(tmp_path, wipe_respond())
    ctx.notes += ["other", "golden image cache-golden-abc", "golden image cache-golden-def"]
    status, answer = call(ctx, "POST", "/runners/runner-1/wipe-store", {})
    assert (status, answer) == (
        200,
        {
            "name": "runner-1",
            "state": "running",
            "golden": "cache-golden-def",
            "device": DEVICE,
            "last_origin_seq": 7,
        },
    )
    check, position, remove, adopt = node.runs
    assert check == ["/bin/sh", "-c", backup.WIPE_SCRIPT, "sh", "/home/u/runner-1"]
    assert "postmaster.pid" in backup.WIPE_SCRIPT
    assert f"WHERE device_id = '{DEVICE}'" in position[-1]
    assert position[position.index("-d") + 1] == "agent_hub"
    assert remove == ["rm", "-rf", "/home/u/runner-1/pg"]
    assert adopt == ["/agent/python", "-m", "agent_runner", "image", "adopt", "cache-golden-def"]
    assert "runner-1" in node.alive_names


def test_wipe_without_a_golden_and_its_failures(tmp_path: Path) -> None:
    ctx, node, _ = wipe_ctx(tmp_path, wipe_respond({"psql": ok("\n")}))
    answer = call(ctx, "POST", "/runners/runner-1/wipe-store", {})[1]
    assert (answer["golden"], answer["last_origin_seq"]) == (None, 0)
    assert not any(argv[3:5] == ["image", "adopt"] for argv in node.runs)
    assert call(ctx, "POST", "/runners/runner-9/wipe-store", {})[0] == 404

    cases: list[tuple[dict[str, Result], str]] = [
        ({"sh": failed(4, "")}, "wipe: exit 4"),
        ({"sh": failed(3, "no identity")}, "wipe: exit 3 no identity"),
        ({"sh": ok("not json")}, "wipe: the runner's device.json names no device"),
        ({"sh": ok("[]")}, "wipe: the runner's device.json names no device"),
        ({"sh": ok('{"device_id": "x\'; DROP"}')}, "wipe: the runner's device.json names no device"),
        ({"rm": failed(1, "busy")}, "wipe: busy"),
    ]
    for overrides, detail in cases:
        ctx, node, _ = wipe_ctx(tmp_path, wipe_respond(overrides))
        status, answer = call(ctx, "POST", "/runners/runner-1/wipe-store", {})
        assert (status, answer["detail"]) == (500, detail), overrides
        assert not any(argv[:2] == ["rm", "-rf"] for argv in node.runs) or overrides.get("rm") is not None
    ctx, _, _ = wipe_ctx(tmp_path, wipe_respond())
    ctx.cluster = None
    assert call(ctx, "POST", "/runners/runner-1/wipe-store", {})[1]["detail"].startswith("wipe: the runner's")

    ctx, _, _ = wipe_ctx(tmp_path, wipe_respond({"adopt": failed(1, "no golden")}))
    ctx.notes.append("golden image cache-golden-abc")
    status, answer = call(ctx, "POST", "/runners/runner-1/wipe-store", {})
    assert (status, answer["detail"].startswith("restart: runner-1: image adopt failed")) == (500, True)


def test_the_last_line_of_empty_output_is_empty() -> None:
    assert backup._last_line({"stdout": "", "stderr": ""}) == ""
