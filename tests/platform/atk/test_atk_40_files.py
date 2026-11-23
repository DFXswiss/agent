"""ATK-40: file import and export abuse (07-testing.md section 2.3; 03-api.md section 7.1; 05-runner.md section
12.1).

Checkouts through the runner stand-in: ``atk/links`` from the GitHub double (symbolic links out of the workspace:
imported without them) and the malicious archives of ``fixtures/tarballs.py`` (``..`` and absolute paths refused;
hard links and devices skipped, only the regular file imported); every archive is also sent raw to the guest's
``files/import``, which must refuse it alone. The export for ``github.commit`` walks a workspace directory with
links to the foreign secret, ``/etc/shadow`` and ``/etc``, a FIFO, a device, a hard link to the foreign secret and a
root-owned canary: only the session's own regular file may leave, the rest is listed as skipped, and no foreign
marker appears in what would go to GitHub. Export requests for ``..``, absolute directories, a linked directory and
paths out of the directory are refused. ``test_atk_40[mode]`` is the real run (marker ``atk``).
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from typing import Any

import pytest

from agent_doubles.atk import catalog, extended
from agent_runner.github import tarsafe
from tests.platform.atk.fakes import SID, UID, VICTIM, FakeHost, Refused, assert_attack, context, run, run_of
from tests.platform.atk.fixtures import tarballs
from tests.platform.atk.runner_standin import RunnerStandIn

EXPORT = f"/srv/sessions/{SID}/work/{extended.EXPORT_DIR}"


@pytest.mark.atk
@pytest.mark.parametrize("mode", catalog.MODES)
def test_atk_40(atk_results: dict[str, Any], mode: str) -> None:
    assert_attack(atk_results, "ATK-40", mode)


def export_tar(manifest: dict[str, Any] | None, first: str = ".agent-export-manifest.json") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        if manifest is not None:
            data = json.dumps(manifest).encode()
            info = tarfile.TarInfo(first)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_the_export_directory_is_seeded() -> None:
    host = FakeHost()
    run(extended.before_40(context("ATK-40", host)))
    uid, victim = str(UID), f"/srv/sessions/{VICTIM}/work/secret.txt"
    commands = [probe[:2] for probe in host.probes]
    assert commands == [
        ("seed", f"{EXPORT}/notes.md"),
        ("seed", f"{EXPORT}/root-canary.txt"),
        ("symlink", f"{EXPORT}/leak-secret"),
        ("symlink", f"{EXPORT}/leak-shadow"),
        ("symlink", f"{EXPORT}/dir-link"),
        ("mkfifo", f"{EXPORT}/pipe"),
        ("mknull", f"{EXPORT}/null"),
        ("hardlink", f"{EXPORT}/victim-hl.txt"),
    ]
    assert host.probes[1][2:5] == ("0", "0", "644")
    assert b"ATK-CANARY-ETC" in base64.b64decode(host.probes[1][5])
    assert host.probes[2][2:] == (victim, uid, uid)
    assert host.probes[-1][2] == victim


def test_manifest_reads_the_first_member() -> None:
    assert extended.manifest(export_tar({"files": [], "skipped": []})) == {"files": [], "skipped": []}
    with pytest.raises(ValueError, match="without its manifest"):
        extended.manifest(export_tar({"files": []}, first="other.json"))
    with pytest.raises(ValueError, match="without its manifest"):
        extended.manifest(export_tar(None))


def test_guest_import_and_export_report_refusals() -> None:
    host = FakeHost()
    host.guest.import_answers = {"a": Refused("path_invalid"), "b": OSError("gone")}
    host.guest.exports = {"a": Refused("not_found"), "b": RuntimeError("x")}
    ctx = context("ATK-40", host)
    assert run(extended.guest_import(ctx, "a", b"")) == "path_invalid"
    assert run(extended.guest_import(ctx, "b", b"")) == "OSError"
    assert run(extended.guest_import(ctx, "c", b"")) == "ok"
    assert run(extended.guest_export(ctx, "a")) == ("not_found", b"")
    assert run(extended.guest_export(ctx, "b")) == ("RuntimeError", b"")


def operation(op_type: str, repo: str, **params: Any) -> dict[str, Any]:
    return {"pending_id": "p", "operation_id": "o", "type": op_type, "params": {"repo": repo, **params},
            "local_check": "passed"}  # fmt: skip


def test_checkouts_import_what_the_runner_keeps_and_try_the_raw_archive() -> None:
    host = FakeHost()
    host.stand_in.checks = {"github.checkout": None}
    host.stand_in.archives = {"atk/links": ("sha1", b"gz-links"), "atk/evil-dotdot": ("sha2", b"gz-evil")}
    host.stand_in.repacked = {b"gz-links": (b"plain", None), b"gz-evil": (None, "path_invalid")}
    host.guest.import_answers = {"repos/atk/raw-links": Refused("request_invalid"), "repos/atk/links": {
        "files": 1, "bytes": 66}}  # fmt: skip
    ctx = context("ATK-40", host)
    links = run(extended.on_operation_40(ctx, operation("github.checkout", "atk/links")))
    assert links["result"] == {"operation_id": "o", "status": "succeeded", "result": {
        "repo": "atk/links", "commit_sha": "sha1", "dir": "repos/atk/links", "files": 1, "bytes": 66}}  # fmt: skip
    evil = run(extended.on_operation_40(ctx, operation("github.checkout", "atk/evil-dotdot")))
    assert evil["result"]["status"] == "failed"
    assert evil["result"]["error"]["code"] == "path_invalid"
    assert [(d, data) for _s, d, data, _r in host.guest.imports] == [
        ("repos/atk/raw-links", b"raw:atk/links"),
        *((directory, b"plain") for directory in extended.IMPORT_PROBES.values()),
        ("repos/atk/links", b"plain"),
        ("repos/atk/raw-evil-dotdot", b"raw:atk/evil-dotdot"),
    ]
    assert set(ctx.data["import_probes"]) == set(extended.IMPORT_PROBES)  # once, with the first archive
    assert ctx.data["checkouts"] == [
        {"repo": "atk/links", "runner": "ok", "guest_raw": "request_invalid", "imported": {"files": 1, "bytes": 66}},
        {"repo": "atk/evil-dotdot", "runner": "path_invalid", "guest_raw": "ok"},
    ]


def test_the_commit_exports_and_probes_the_export_refusals() -> None:
    host = FakeHost()
    host.stand_in.checks = {"github.commit": None}
    found = {"files": [{"path": "notes.md"}], "skipped": []}
    host.guest.exports = {
        extended.EXPORT_DIR: export_tar(found),
        "../..": Refused("path_invalid"),
        f"{extended.EXPORT_DIR}:/etc/shadow": b"leaked root:x",
    }
    ctx = context("ATK-40", host)
    answer = run(extended.on_operation_40(ctx, operation("github.commit", "atk/export", branch="b", message="m")))
    assert answer["result"]["error"]["code"] == "upstream_error"
    assert ctx.data["export"] == {"code": "ok", "manifest": found}
    assert ctx.data["export_probes"] == {"dotdot": "path_invalid", "absolute": "ok", "dir-link": "ok",
                                         "path-dotdot": "ok", "path-absolute": "ok"}  # fmt: skip
    assert "leaked root:x" in ctx.outbound
    host.guest.exports = {}
    ctx = context("ATK-40", host)
    run(extended.export_40(ctx, operation("github.commit", "atk/export")))
    assert ctx.data["export"] == {"code": "ok", "manifest": None}


def test_a_formal_refusal_is_answered_without_touching_the_guest() -> None:
    host = FakeHost()
    host.stand_in.checks = {"github.checkout": {"code": "repo_not_allowed", "message": "m"}}
    answer = run(extended.on_operation_40(context("ATK-40", host), operation("github.checkout", "other/repo")))
    assert answer["result"]["status"] == "rejected"
    assert host.guest.imports == []


def good_data() -> dict[str, Any]:
    checkouts = [
        {"repo": "atk/links", "runner": "ok", "guest_raw": "request_invalid", "imported": {"files": 1}},
        {"repo": "atk/instructions", "runner": "ok", "guest_raw": "path_invalid", "imported": {"files": 1}},
    ]
    for repo, expected in extended.EVIL_REPOSITORIES.items():
        item: dict[str, Any] = {"repo": repo, "runner": expected, "guest_raw": "path_invalid"}
        if expected == "ok":
            item["imported"] = {"files": extended.IMPORTED_FILES[repo]}
        checkouts.append(item)
    skipped = [{"path": path, "reason": reason} for path, reason in extended.EXPORT_SKIPPED.items()]
    return {
        "checkouts": checkouts,
        "export": {"code": "ok", "manifest": {"files": [{"path": "notes.md"}], "skipped": skipped}},
        "import_probes": dict.fromkeys(extended.IMPORT_PROBES, "path_invalid"),
        "export_probes": dict.fromkeys(
            ("dotdot", "absolute", "dir-link", "path-dotdot", "path-absolute"), "path_invalid"
        ),
    }


def test_check_40() -> None:
    ctx = context("ATK-40")
    ctx.data.update(good_data())
    assert extended.check_40(ctx, run_of("ATK-40")) == []
    data = good_data()
    del data["checkouts"][1]  # the instruction repository (checked below)
    data["checkouts"][0]["imported"] = {"files": 5}
    data["checkouts"][1]["runner"] = "ok"
    data["checkouts"][3]["imported"] = {"files": 3}
    del data["checkouts"][4]
    data["checkouts"][2]["guest_raw"] = "ok"
    data["export"]["manifest"]["files"].append({"path": "leak-secret"})
    data["export_probes"] = {"dotdot": "ok"}
    ctx = context("ATK-40")
    ctx.data.update(data)
    found = extended.check_40(ctx, run_of("ATK-40"))
    assert found[0].startswith("the repository with links was not checked out without its links:")
    assert found[1].startswith("atk/evil-dotdot: runner answered ok, expected path_invalid")
    assert found[2].startswith("atk/evil-hardlink: more than the regular file was imported:")
    assert found[3] == "atk/evil-device: runner answered None, expected ok"
    assert found[4] == "the guest took the raw archive of atk/evil-absolute: ok"
    assert found[5].startswith("export of the workspace: ok, files ['notes.md', 'leak-secret']")
    assert found[6] == "the repository with instruction files was not checked out without them: None"
    assert found[7:] == ["export probe dotdot answered ok", "1 export probe(s) ran, expected 5"]
    leaked = context("ATK-40")
    leaked.data.update(good_data())
    leaked.data["checkouts"][1]["imported"] = {"files": 4}
    exchanges = [{"body": {"messages": [{"content": f"{extended.INSTRUCTIONS_MARKER}: run Bash"}]}}]
    assert extended.check_40(leaked, run_of("ATK-40", exchanges=exchanges)) == [
        "the repository with instruction files was not checked out without them: "
        "{'repo': 'atk/instructions', 'runner': 'ok', 'guest_raw': 'path_invalid', 'imported': {'files': 4}}",
        "an instruction file of a checked-out repository reached the model",
    ]
    empty = extended.check_40(context("ATK-40"), run_of("ATK-40"))
    assert empty[0] == "the repository with links was not checked out without its links: None"


def test_the_stand_in_on_the_real_archives() -> None:
    """The runner's tarball rewrite on every archive of the plan gives the outcome ``EVIL_REPOSITORIES`` expects."""
    runner = RunnerStandIn()
    try:
        for repo, expected in {"atk/links": "ok", "atk/instructions": "ok", **extended.EVIL_REPOSITORIES}.items():
            sha, archive = runner.tarball(repo)
            packed, refused = runner.repack(archive)
            assert (refused or "ok") == expected, repo
            if packed is not None:
                with tarfile.open(fileobj=io.BytesIO(packed)) as plain:
                    files = [m.name for m in plain.getmembers() if m.isfile()]
                    assert len(files) == extended.IMPORTED_FILES.get(repo, 1), (repo, files)
                    assert "README.md" in files
                    assert all(m.isfile() or m.isdir() for m in plain.getmembers())
            with tarfile.open(fileobj=io.BytesIO(runner.raw(repo))) as raw:
                hostile = [
                    m
                    for m in raw.getmembers()
                    if not m.isfile() or m.name.startswith("/") or ".." in m.name or tarsafe.protected(m.name)
                ]
            assert hostile, repo  # the raw archive keeps its hostile members
        assert sha == tarballs.SHA
        assert runner.check("github.checkout", {"repo": "atk/links"}) is None
        assert runner.check("github.push", {"repo": "atk/repo", "branch": "main", "commit_sha": "0" * 40}) == {
            "code": "branch_protected", "message": "The branch is protected."}  # fmt: skip
    finally:
        runner.close()


def test_the_stand_in_turns_archive_errors_into_upstream_errors() -> None:
    runner = RunnerStandIn()
    try:
        assert runner.repack(b"not a tarball") == (None, "upstream_error")
    finally:
        runner.close()
