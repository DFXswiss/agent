# ADR-104: A task looks like Claude Code started locally

Status: Accepted (2026-11-27; revised the same evening for the client's acceptance criterion)

Decision for [E-43](../KONZEPT.md#e-43). Changes the default view of [§6 Punkt 6](../KONZEPT.md#k-6-b6); keeps
[§6 Punkt 7](../KONZEPT.md#k-6-b7) (the terminal view is read-only) and [ADR-016](016-stored-read-only-terminal.md)
(no keystroke path) unchanged.

## Context
- Every session runs the interactive Claude Code TUI in a tmux pane of 160 × 48
  ([ADR-025](025-interactive-tui-with-gateway-metrics.md)); its output is recorded and shown read-only with xterm.js
  ([ADR-016](016-stored-read-only-terminal.md)).
- The client wants the task page to look like Claude Code started locally once the session exists, the first input
  (home composer) unchanged; of terminal view with an input (A), chat restyled (B) and an interactive terminal (C) the
  client chose A, and set as acceptance: indistinguishable from the local terminal in the browser – the whole area,
  one input only, the same operation as far as the security model allows, also on a phone.
- Claude Code 2.1.330 draws a full-screen TUI (alternate screen): the conversation from the top, at the bottom of the
  pane its input box – a rule `─`, the prompt marker `❯`, a rule – and below it the status line (mode and hints); it
  scrolls its conversation itself on mouse wheel events.

## Decision
1. **Whole window.** The task page opens on the tab **Claude Code** (`?tab=terminal`, absent `tab` = this tab). While
   it is open the page is the terminal over the whole window (`fixed inset-0`) with one slim dark bar: back to the
   tasks, the title, the tabs. Header, task menu and notices are on every other tab. The screen is the real TUI output
   (`TerminalView` variant `task`): no label, no mode controls, no frame; theme, terminal font (SF Mono, Menlo,
   Monaco, Cascadia Mono, Consolas, DejaVu Sans Mono, Liberation Mono) and line height 1 of a local terminal; the font
   fits all columns and rows into the window, on a phone the rows into its height (the columns scroll sideways).
2. **One input, where Claude Code has it.** After every write the view reads its screen and finds the TUI's input box
   with the guest's rule (`input_box` of `src/agent_guest/delivery.py`: the last `❯` row below a `─` rule, up to the
   next rule; `core/terminal/screen.ts`). The web input (`ClaudeInput`) is drawn exactly over these rows – the rules
   and the marker copied with the colours the TUI drew, the text in the cell grid – and grows upwards with the text as
   the TUI's box does; the TUI's own status line below stays visible. So the page never shows two inputs. While the
   screen shows no box (the start, an agent that ended) the same input is docked below the screen. On a phone its text
   stays in view while the columns scroll (sticky). Failures and "offline" are lines in the TUI's style (`⎿`).
3. **Operation.** Enter sends the text as a signed `prompt` (the chat's path, `useComposer`); Shift+Enter or Alt+Enter
   start a line; Enter while an IME composes belongs to the IME; Esc anywhere on the page interrupts the main session's
   turn (`POST /v1/sessions/{id}/control {"action":"interrupt"}`, the guest presses `Escape` in the pane). Nothing reaches
   the terminal as a keystroke (ADR-016).
4. **Start.** Until the main session works, and after its start failed, the start is shown as terminal lines above
   the screen (countdown, current step, waiting reason with its action, steps; "Retry" for writers).
5. The chat stays complete as the second tab; the session detail keeps its Terminal tab with history and replay. The UI
   health report names the tab `task.terminal` (`contracts/enums/ui-views.json`, change note `contracts/changes/E-43.md`).

## What stays different from Claude Code locally
Without a keystroke path into the pane (§6 Punkt 7, ADR-016) these do not work in the browser:
- **Scrolling the conversation inside the TUI.** Claude Code 2.1.330 scrolls on mouse wheel events it reads from the
  pane; the browser sends none. Earlier output: the Chat tab, or the session detail's History (replay).
- **Keys other than Enter, Shift+Enter, Alt+Enter and Esc:** Shift+Tab (mode cycling), arrow keys (prompt history,
  menus), Tab and `@` completion, Ctrl+R, Ctrl+C / Ctrl+D, `?` shortcut help, Ctrl+O transcript, pasting images.
- **Slash commands and `!` bash mode:** the platform starts Claude Code with `--disable-slash-commands`, and the guest
  refuses texts that Claude Code would run as a command (`deliverable`), since they might bypass the prompt hook.
- **Interactive menus of the TUI** (a question with options): answered in the chat / with the platform's own cards, not
  with arrow keys in the terminal.
- **Typing while the guest delivers:** the prompt appears in the TUI's own box only for the moment the guest pastes it;
  the web input is already empty then.
- **The caret** is the browser's text caret, not the TUI's block cursor.
- **Phone width:** the pane has 160 columns for every viewer and the recording (ADR-016), so a phone shows them at a
  readable size and scrolls sideways; the TUI cannot be re-laid out per viewer.

## Consequences
- The home send sequence (WEB-04) navigates to the task page, which opens on Claude Code with the start lines.
- Tests and E2E specs that rely on the chat or the header open the Chat tab (`openChat`).
- The input's geometry depends on the TUI's box rule; when a Claude Code upgrade changes the box, the input docks below
  the screen (no box found) until `core/terminal/screen.ts` follows – the Claude Code upgrade gate covers it with the
  recorded screen of the pinned version (`web/e2e/fixtures/claude-code-2.1.330.out`).
- Evidence (local terminal and browser side by side, Playwright): [acceptance/E-43](../acceptance/E-43/README.md).

## Alternatives considered
Chat restyled like the CLI (B; not chosen); keystrokes into the terminal (C; contradicts §6 Punkt 7 and ADR-016, not
chosen); an input box below the terminal in addition to the TUI's own box (the first version of this ADR: two inputs,
rejected by the client's acceptance).

## Deviation from DESIGN.md
None.
