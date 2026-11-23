# ADR-021: Claude Code as the only agent software in v1 (KONZEPT §4)

Status: Accepted (2026-11-21)

## Context
KONZEPT §4: Claude Code is the only agent software in version 1; later support of other agent software is optional. The sandbox, hooks and managed settings (ADR-008) are specific to Claude Code 2.1.x.

## Decision
- v1 supports only Claude Code, pinned per golden image.
- The session record carries `agent_kind` (`claude-code`) and `agent_version`; the runner/guest encapsulate agent-specific behaviour (argv, managed settings, hooks, transcript parsing, prompt delivery) behind an `AgentDriver` interface with exactly one implementation.
- Other agent software is **not** planned for v1; adding one requires its own sandbox proof (attack suite) and an ADR.

## Consequences
Minimal scope; the interface keeps a later extension local to runner and guest without hub or API changes.

## Alternatives considered
Generic driver tables like legacy `drivers.json` (operator-configured, not provable against the sandbox requirements).

## Deviation from DESIGN.md
None (DESIGN's vendor neutrality applies to the legacy CLI; the platform is Claude-specific by KONZEPT).
