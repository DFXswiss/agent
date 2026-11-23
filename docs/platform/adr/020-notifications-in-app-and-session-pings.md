# ADR-020: In-app notifications and content-free session pings (KONZEPT §12)

Status: Accepted (2026-11-21)

## Context
KONZEPT §12: when creating a whiteboard entry the author may notify the entitled participants of that level; active cloud sessions only receive a content-free ping that a new entry is available; the session decides itself whether to fetch it; no automatic interruption or content transfer. Legacy "knock" types a line into the TUI (would interrupt and act as a prompt).

## Decision
- Users: `notify:true` on `whiteboard.entry` creates content-free `notification` rows for every user with read right on the level (except the author), delivered live via SSE and listed in the in-app notification centre (badge, list, mark read). **No e-mail and no web push in v1** (the hub would need addresses or push endpoints; a later ADR may add opt-in channels).
- Sessions: the hub sends a `ping` hint to devices running non-stopped sessions in scope (org level: all sessions of the org; workgroup level: of the workgroup; task level: of the task); the guest stores the ping and attaches a fixed, content-free `additionalContext` notice to the **next** hook answer (PreToolUse, PostToolUse, UserPromptSubmit). The session may call `get_notifications` and then `get_whiteboard`. Nothing is ever typed into the TUI. Idle sessions see the notice with their next prompt.
- Same mechanism for `subagent.finished` and `operation.finished` pings.

## Consequences
- Users who are not in the app are not reached outside it in v1.
- Hook `additionalContext` support of the pinned Claude Code version is verified in M0 (fallback: `get_notifications` only).

## Alternatives considered
Typing a knock line into the TUI (interrupts, counts as prompt), e-mail with content (violates §5), web push (infrastructure, later).

## Deviation from DESIGN.md
None (DESIGN session-mail knock is not used for platform sessions).
