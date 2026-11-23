# ADR-051: The subscription login reads the terminal screen of `claude setup-token`

Status: Accepted (2026-11-23)

## Context
- The setup-token relay (ADR-011, 05-runner.md §11.2) runs `claude setup-token` in a PTY and used to read its raw
  output with the control sequences stripped. It had only ever run against the login-CLI double, which printed whole
  lines.
- A recording of the real program (Claude Code 2.1.330, the golden's pin; KOR-21) showed three defects:
  1. The program places words with cursor positioning (`ESC[<n>G`) instead of spaces, erases lines (`ESC[2K`,
     `ESC[K`), moves the cursor (`ESC[<n>A/B/C/D`) and overwrites single changed cells. Stripping the sequences turns
     "Your OAuth token (valid for 1 year):" into `YourOAuthtoken(validfor1year):`; the token block is not found and a
     successful authorization ends `token_invalid`. Without an OpenRouter key the user then has no model access.
  2. A refused code does not end the program: it shows "✘ OAuth error: That code wasn't accepted. …" and "Press Enter
     to retry" and waits. The runner waited for the end of the program (`token_wait`, 60 s) and answered `timeout`
     instead of `code_invalid`.
  3. The input field ("Paste code here if prompted") appears about 3–4 s after the URL. A code typed earlier is
     dropped (possible with the mailbox automation, §11.3); the program keeps waiting.

## Decision
1. **Screen reconstruction.** The runner replays the PTY output on its own small terminal screen of the PTY's window
   size (`agent_runner.login.screen`, standard library only): printable characters (wide characters take two cells,
   combining characters join the previous cell, deferred wrap at the right margin), CR, LF with scrolling, BS, HT,
   ESC 7/8, CSI `A B C D E F G H f d J K` with parameters and defaults; SGR, private modes, queries, OSC/DCS/APC
   strings and every other sequence have no effect. Outside that scope (sequences the program does not send) the
   screen may differ from xterm: a C0 control inside an ESC sequence, CAN/SUB, a private marker in the middle of the
   parameters, a combining character after a space, the width of U+00AD. The parser keeps its state across reads; `PtyProcess` decodes
   UTF-8 incrementally. No scrollback: the login's frames fit the 50-row window. Every decision of the login reads the
   screen's rows, never the raw output.
2. **Labels in one place.** `agent_runner.login.setup_token` holds the program's labels ("Paste code here if
   prompted", "OAuth error", "OAuth token") and `CHECKED_VERSION`, the Claude Code version they were taken from. A
   test ties `CHECKED_VERSION` to `claude_code` in `images/versions.json`, so moving the pin fails until the login has
   been checked against the new version (`runbooks/claude-code-upgrade-gate.md`).
3. **Wait for the input field** before typing the code (`prompt_wait`, 30 s; `timeout` when it never shows,
   `login_cli_failed` when the program ended).
4. **Error while running.** After the code, "OAuth error" on the screen ends the login at once with `code_invalid`
   (the program is killed by the cleanup). Every "OAuth error" counts as a refused code, as before (the double and the
   contract know no finer distinction). Otherwise the program must end within `token_wait`.
5. **Token from the final screen.** Only after the program ended (it ends by itself 500 ms after showing the token),
   so no half-drawn frame is read. With the marker row: the first run of non-empty rows after it, stripped and joined,
   must be a whole token. **Second, strict way without the marker** (decided for KOR-21): exactly one run of non-empty
   rows on the whole screen that is, joined, a token and nothing else. A login that has succeeded is valuable and a
   later version may reword the marker; a run that is a whole token and nothing else can only be the token, and a
   second candidate, a run with other text or a row cut by the renderer ("…" is not a token character) yields
   `token_invalid`. In both ways the token's run must be the only run on the screen that looks like a token or a
   piece of one (token characters only, with the prefix or at least 16 long), so a token split by an empty row (a
   row blanked by a later version or a sequence the screen does not know) is refused, not stored truncated. A part
   that stands alone on an otherwise correct screen cannot be told from a short token; the format check and reading
   only the final screen are the limit here.
6. **Diagnosis without secrets.** A failure after the start carries the form of the screen (rows, marker found,
   number of token-shaped runs, input field and error shown) to the runner log, never text, URL, code or token.
7. **The login-CLI double draws like the real program** (cell-by-cell frames, positioning instead of spaces,
   spinner, raw input mode without echo, delayed input field that drops earlier input, stays open after an error,
   ends by itself after the token). Tests can no longer pass because the double is friendlier than the original.
8. **Typing settles.** The code is typed 0.3 s after the input field shows (the program may enable its input right
   after drawing it); an error shown before the code ends the login with `login_cli_failed`.

## Consequences
- The relay works with the recorded program; the probe against the real program (wrong code, no account) answers
  `code_invalid` about 0.2 s after typing, and the code is typed only once the field shows (KOR-21 report).
- The success path against the real program cannot run without an account (ADR-011: no live login); it is covered by
  screens drawn the way 2.1.330 draws, including a frame overwritten cell by cell and a token wrapped in a narrow
  window.
- A Claude Code upgrade must re-check the labels and the drawing (upgrade gate).

## Alternatives considered
- Insert spaces for cursor jumps while stripping: breaks on cursor-up, erase and cell overwrites (stale frames and
  partly overwritten words stay in the text).
- A terminal emulator library (`pyte`): a new dependency for a few sequences; the screen needed here is small and
  fully tested.
- Make the program print plain text (`TERM=dumb`, no TTY): the program needs a TTY for its input; its renderer does
  not promise plain output.

## Deviation from DESIGN.md
None.
