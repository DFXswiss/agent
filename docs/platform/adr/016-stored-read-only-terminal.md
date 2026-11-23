# ADR-016: Stored, read-only terminal view

Status: Accepted (2026-11-21)

## Context
- KONZEPT §6: an additional native Claude Code session view with complete live terminal output **and history**, explicitly read-only; prompts only via the chat view. §13: same for subagent sessions.
- Legacy: terminal bytes are ephemeral (64-chunk ring in hub memory) and the hub relays `control` frames with arbitrary `command` and keyboard `input`.
- Spike B: tmux `pipe-pane` → asciicast v2 recorder; read-only tmux clients cannot type; typed text in the pane is refused by the UserPromptSubmit hook.

## Decision
- Each session runs in a root-owned tmux server inside the VM (160×48); output is recorded as asciicast v2 output frames (incremental UTF-8 decoding, monotonic offsets across recorder restarts), journaled, encrypted by the runner as `session.terminal` events (≤ 64 KiB, flushed every 100 ms) and stored at the hub like all events.
- The web client renders live frames and full history with xterm.js with input disabled.
- **There is no terminal input path**: no hub endpoint, WebSocket frame or guest endpoint accepts keystrokes. Platform controls are limited to `interrupt` and `stop` (`POST /v1/sessions/{id}/control`). Prompts are delivered only by the runner after verification of a signed `prompt` event.

## Consequences
- Terminal history survives hub restarts and is readable by workgroup readers.
- Storage grows with terminal volume (no retention limit in v1; measured in M3).

## Alternatives considered
Ephemeral relay (no history), stream-json transcript rendering only (not native view), owner typing via signed commands (contradicts read-only).

## Deviation from DESIGN.md
Yes: §17 (control `input` frames; terminal bytes not stored), §19.4 and §23 (ephemeral terminal bytes), §25.9 (session log masked but readable at the hub) and §25.10 (typing into sessions through the hub).
