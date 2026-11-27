# ADR-104: The Claude Code view is the default tab of a task

Status: Accepted (2026-11-27)

Decision for [E-43](../KONZEPT.md#e-43). Changes the default view of [§6 Punkt 6](../KONZEPT.md#k-6-b6); keeps
[§6 Punkt 7](../KONZEPT.md#k-6-b7) (the terminal view is read-only) and [ADR-016](016-stored-read-only-terminal.md)
(no keystroke path) unchanged.

## Context
- Every session already runs the interactive Claude Code TUI in tmux ([ADR-025](025-interactive-tui-with-gateway-metrics.md));
  its output is recorded and shown read-only with xterm.js ([ADR-016](016-stored-read-only-terminal.md)) – but only in
  the session detail, one level below the task page, while the task page opened on the chat.
- The client wants the task to look like Claude Code started locally once the session exists; the first input (home
  composer) stays as it is. Of the options offered – terminal view with an input below (A), the chat restyled like the
  CLI (B), a truly interactive terminal (C) – the client chose A.

## Decision
1. The task page gets a first tab **Claude Code** (`?tab=terminal`, absent `tab` = this tab; registry order 5, before
   Chat). It shows the terminal of the task's main session (`TerminalView` variant `task`, the same lazy chunk as the
   session detail) and follows a new main session after a resume or failover. The page is wider (`max-w-6xl`) while it
   is open, so the recorded 160 columns stay readable.
2. Below the terminal the prompt input in the style of Claude Code (`ChatComposer` variant `cli`): a dark monospace box
   with the `>` sign, Enter sends, Shift+Enter adds a line, Ctrl/⌘+Enter sends too; Stop and End session as in the chat.
   A prompt is still a signed `prompt` event delivered by the runner; nothing reaches the terminal as keystrokes.
3. Until the main session works (or after its start failed) the countdown panel of the start (06-web.md §4.4, Retry for
   writers) sits above the terminal; before the main session exists a dark placeholder stands in for the terminal.
   The model access card and the queue of waiting instructions appear as in the chat; readers get no input.
4. The chat stays complete as the second tab (`?tab=chat`); the session detail keeps its Terminal tab.
5. The UI health report names the tab as the view type `task.terminal` (`contracts/enums/ui-views.json`, change note
   `contracts/changes/E-43.md`).

## Consequences
- The home send sequence (WEB-04) still navigates to the task page; it now opens on the Claude Code tab with the
  countdown.
- Tests and E2E specs that relied on the chat being the default open `?tab=chat`; links into the chat should name it.
- On a phone the 160-column terminal scrolls horizontally (as in the session detail); the chat tab remains the more
  compact view there.

## Alternatives considered
Chat restyled like the CLI (B; not chosen, may follow); keystrokes into the terminal (C; contradicts §6 Punkt 7 and
ADR-016, not chosen).

## Deviation from DESIGN.md
None.
