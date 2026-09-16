"""Adopter checklist in docs/a38.md — no Agent runtime import."""

from pathlib import Path


def _a38_md() -> str:
    return (Path(__file__).resolve().parents[1] / "docs" / "a38.md").read_text(
        encoding="utf-8"
    )


def test_adopter_guide_is_the_checklist_not_a_second_engine() -> None:
    text = _a38_md()
    assert "## Adopting A38 in a repository" in text
    for path in (
        ".github/a38.json",
        ".github/pr-guard.json",
        ".github/workflows/a38-guard.yml",
    ):
        assert path in text
    assert "examples/a38-guard.yml" in text
    assert "USES_REF_PIN_ME" in text
    assert "agent a38 policy --file .github/a38.json" in text
    assert "A38-POLICY-APPROVAL:v1" in text
    lowered = text.lower()
    assert "yaml interpreter" in lowered or "step runner" in lowered
    assert "do not point `steps` at a helper that re-parses the workflow yaml" in lowered
    assert "copying this document" in lowered
    guard_md = (Path(__file__).resolve().parents[1] / "docs" / "a38-guard.md").read_text(
        encoding="utf-8"
    )
    assert "a38.md#adopting-a38-in-a-repository" in guard_md
    assert "if the default branch is `main`, list `main` in `a38.enforce`" in lowered
