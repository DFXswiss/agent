# ADR-025: Interactive TUI with gateway-based metrics

Status: Accepted (2026-11-21)

## Context
- KONZEPT §6 requires a native Claude Code terminal view (read-only) next to the chat; Spike B ran Claude Code interactively in tmux.
- Spike C recommended `--input-format/--output-format stream-json` for robust injection and metrics (`stream_event` cost, `get_context_usage`), but that mode has no native terminal view.

## Decision
- Sessions run the **interactive TUI** in tmux (ADR-016).
- Metrics come from the **model gateway** (tokens, cost, provider, context per request; ADR-010); chat outputs and tool events from the **transcript JSONL** read by the root control process plus HTTP hooks; file changes from PostToolUse plus a periodic workspace scan.
- Prompt delivery: register the prompt hash, then initial prompt as argv on start; later prompts typed with `tmux send-keys -l` in 40-character chunks when single-line and ≤ 200 characters, otherwise as bracketed paste; Enter with capture-pane verification (≤ 3 retries). The UserPromptSubmit hook accepts only registered hashes (normalising the pasted-content wrapper).

## Consequences
- Native view and metrics without trusting the session.
- TUI automation depends on Claude Code's input behaviour; covered by E2E on every Claude Code upgrade.

## Alternatives considered
stream-json only (no native view), stream-json plus a rendered pseudo-terminal (not native), statusline command for context % (needs an exec exception, Spike B).

## Deviation from DESIGN.md
None.
