from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli.main import main, packaged_skills_dir
from agent_cli.skills import SKILL_NAMES

pytestmark = pytest.mark.no_pg


def run(argv: list[str]) -> None:
    main(argv)


def test_error_fix_is_a_named_skill() -> None:
    assert "error-fix" in SKILL_NAMES


def test_skills_path_packaged(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)
    monkeypatch.delenv("AGENT_HOME", raising=False)
    run(["skills", "path"])
    out = capsys.readouterr().out.strip()
    assert out
    assert "\n" not in out
    root = Path(out)
    assert root.is_dir()
    assert (root / "spine" / "SKILL.md").is_file()
    assert (root / "review-loop" / "SKILL.md").is_file()
    assert (root / "pr-review" / "SKILL.md").is_file()
    assert (root / "error-fix" / "SKILL.md").is_file()


def test_skills_path_with_agent_home(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    run(["skills", "path"])
    assert (Path(capsys.readouterr().out.strip()) / "spine" / "SKILL.md").is_file()


def test_skills_path_env_override(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("spine", "review-loop", "pr-review"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(tmp_path))
    run(["skills", "path"])
    assert Path(capsys.readouterr().out.strip()) == tmp_path.resolve()


def test_skills_path_empty_env_falls_back(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SKILLS_DIR", "")
    run(["skills", "path"])
    assert Path(capsys.readouterr().out.strip()) == packaged_skills_dir().resolve()


def test_skills_path_invalid_env_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="AGENT_SKILLS_DIR does not contain"):
        run(["skills", "path"])


def test_skills_path_incomplete_override_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "spine").mkdir()
    (tmp_path / "spine" / "SKILL.md").write_text("# spine\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="AGENT_SKILLS_DIR does not contain"):
        run(["skills", "path"])


def test_skills_path_missing_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_SKILLS_DIR", raising=False)
    monkeypatch.setattr("agent_cli.main.packaged_skills_dir", lambda: tmp_path / "missing")
    with pytest.raises(SystemExit, match="skill docs are not installed"):
        run(["skills", "path"])


def test_skills_path_usage() -> None:
    with pytest.raises(SystemExit, match="Usage: agent skills path"):
        run(["skills"])
    with pytest.raises(SystemExit, match="Usage: agent skills path"):
        run(["skills", "list"])


def test_the_documented_rejected_gate_command_carries_evidence() -> None:
    # The contract's own example is what an operator copies. A `rejected` form
    # without `--evidence` exits before recording the gate or queuing its findings.
    contract = (packaged_skills_dir() / "pr-review" / "SKILL.md").read_text()
    rejected = [ln for ln in contract.splitlines() if "--verdict rejected" in ln]
    assert rejected, "the contract shows no rejected gate example"
    assert any("--evidence" in ln for ln in rejected), rejected
