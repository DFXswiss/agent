"""Named opt-in skills. None are attached by default."""

from __future__ import annotations

SKILLS = {
    "spine": {
        "version": "1",
        "tables": ("task", "task_round", "checklist_item", "local_check", "open_work"),
        "commands": (
            "task",
            "checklist",
            "round",
            "check",
            "work",
            "allow",
            "next",
            "close-step",
            "run",
        ),
    },
    "review-loop": {
        "version": "1",
        "tables": ("agent",),
        "commands": ("agent",),
        "roles": ("implementer", "reviewer"),
    },
    "pr-review": {
        "version": "1",
        "tables": ("review_gate",),
        "commands": ("gate",),
        "roles": ("pr-reviewer-quality", "pr-reviewer-logic"),
    },
    "error-fix": {
        "version": "1",
        "tables": (),
        "commands": (),
    },
}

SKILL_NAMES = tuple(SKILLS)


def skill_for_agent_role(role: str) -> str:
    if role in ("implementer", "reviewer"):
        return "review-loop"
    if role in ("pr-reviewer-quality", "pr-reviewer-logic"):
        return "pr-review"
    raise ValueError(f"unknown agent role: {role}")


def session_skills(session: dict) -> list[str]:
    raw = session.get("skills") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item in SKILLS]


def has_skill(session: dict, name: str) -> bool:
    return name in session_skills(session)
