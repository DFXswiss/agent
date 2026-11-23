# KOR-21 – Subscription login against the real `claude setup-token`: read the screen

Follow-up after the release: the setup-token relay (05-runner.md §11.2) had only run against the login-CLI double.
A recording of the real program (Claude Code 2.1.330) showed that the token is not found after a successful
authorization, that a wrong code ends in `timeout` after 60 s instead of `code_invalid`, and that a code typed before
the input field shows is lost. ADR-051 records the design; this file records the smaller decisions.

## Decisions

1. **Own screen, no library.** `agent_runner.login.screen` implements exactly the sequences the program uses
   (ADR-051 §1). `pyte` would be a new dependency of the runner for a dozen sequences; the module is small and tested
   sequence by sequence and against the real recording. ESC 7/8 (save and restore the cursor) are implemented
   although the work package did not list them: the recording starts with `ESC 7 ESC[r ESC 8`, and keeping the cursor
   is what a terminal does. `CSI 3 J` clears only the scrollback of a terminal, so it has no effect here.
2. **No scrollback.** The program's frames are about 25 rows; the window stays 50 x 1000. Keeping rows that scrolled
   off would bring stale frames back into the search for the token.
3. **The raw output buffer stays** (`PtyProcess.output`, bounded as before). The login no longer reads it; it is kept
   for the PTY unit tests and costs nothing new (the token was in it before as well).
4. **Where the labels live.** `setup_token.py` holds the labels and `CHECKED_VERSION`. This is a record of what was
   checked, not a second source of the pin: the test `test_the_labels_were_checked_against_the_pin_of_the_golden`
   fails when `images/versions.json` moves, which is the point (upgrade gate §1 step 4).
5. **Timeout codes.** No input field within `prompt_wait` → `timeout` (the program may only be slow, like a missing
   URL); the program ended before showing it → `login_cli_failed`. `prompt_wait` counts from the code, so a user who
   sends the code late loses nothing.
6. **Every "OAuth error" is `code_invalid`.** The program shows other OAuth errors the same way (e.g. a failed
   request); the response contract has no finer code, and the relay already mapped `OAuth error` to `code_invalid`.
7. **Strict second way without the marker: yes** (ADR-051 §5). A complete token is only taken from the final screen,
   after the program ended; a run of rows must be a whole token and nothing else. A token shorter than the format's
   minimum, a row cut with "…", text in the same run or a second candidate yields `token_invalid`.
8. **The double turns its terminal to raw mode** (no echo, no line editing; output processing stays on, as Node's raw
   mode does). With echo on, the PTY wrote the typed code into the drawn screen and broke the double's cell-by-cell
   frames – a real difference to the program that tests must not hide.
9. **The double's prompt delay** is 1.5 s by default (the real program: about 3 s). Every login test waits for it, the
   E2E suite included; the test that sends the code before the field uses 8 s.
10. **After an error the double stays open** until `--code-timeout` or the end of its input and ignores Enter (the
    real program would start a new sign-in). The runner never presses Enter: it ends the login on the error.
11. **Probe on a development machine.** The work package asked for the probe on the second device. It was not
    reachable from the development machine on the day (another network). The probe ran on the development machine
    (macOS arm64) with exactly the runner's isolation (`LoginSession`: temporary `HOME`, `CLAUDE_CONFIG_DIR`, `TMPDIR`,
    allow-list environment, `BROWSER` capture helper), Claude Code 2.1.330 installed with `npm install --prefix` into
    a scratch directory of the work, no account, a deliberately wrong code. Results in ADR-051 and FINAL.md; the
    scratch directory was removed afterwards. Afterwards the keychain's metadata (no secrets read) showed no Claude
    Code entry created or modified during the probe; the runner's isolation keeps the program away from existing
    profiles.
12. **E2E-28 not run.** The runner device of the E2E topology is a production device now and was off limits for this
    work. E2E-28 drives the same double through the web; the double's new behaviour is covered by the runner's request
    suite (`tests/platform/runner/login/`) through the real request service. It runs with the next E2E run.

13. **`test_legacy_untouched.py`** (both repositories): since the platform was merged into `develop`, its files exist
    on the base and change there; the guard refused every modified platform file. The fix is byte-identical to the one
    of KOR-20 (which runs in parallel), so both changes merge without conflict.

## Not changed

`relay.resolve()`, `doctor/` and the runbooks `runner-installation.md`/`golden-image-release.md` belong to KOR-20
(host prerequisite, doctor check `login_cli`), which runs in parallel. `05-runner.md` §11.2 steps 2–4 are unchanged
for the same reason; the new rules follow step 5.
