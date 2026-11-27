# RUN-13 decisions (Claude subscription login: setup-token relay and mailbox automation)

Small decisions of the package that stores a `claude_subscription` credential from the execution device by the
setup-token relay or the mailbox automation and answers `credential.check` and `mailbox.check` (05-runner.md §1.3,
§11.1–§11.3; 04-crypto.md §4.4, §4.5; 03-api.md §4.10, §6.1; ADR-011). No decision changes an architecture rule, so
there is no ADR.

## Request handling
1. The three handlers live in `agent_runner/login/requests.py:HANDLERS` and use the RUN-09 request frame unchanged:
   one flow per user and kind (a newer root replaces and cancels the running one, an older one is `busy`), the
   request's 15 minutes, follow-ups routed to the running root of the same user and kind (a follow-up of another user
   is refused and audited `forbidden` by the frame; the flow goes on), gapless signed responses.
2. The final answer goes to the newest request of the chain: in the relay the follow-up that carried the code
   (`{step:"done"}` or `{step:"failed", code}`), otherwise the root. The hub closes the whole chain on any final
   response (03-api.md §4.10).
3. A root body other than exactly `{mode: "manual"|"automatic"}` (and a `credential.check`/`mailbox.check` body that
   does not fit its schema) is answered `upstream_error`: the closed failure list has no "invalid request" code
   (same reasoning as RUN-09 decision 7). A malformed follow-up (`code` not printable ASCII ≤ 512, or other keys) is
   `code_invalid`.

## Relay (`relay.py`, `pty.py`, `capture.py`)
4. Temporary directory `$AGENT_RUNNER_HOME/tmp/login-<root request id>` (0700, `tmp/` created 0700 when missing) with
   `home/`, `config/`, `tmp/` as `HOME`, `CLAUDE_CONFIG_DIR`, `TMPDIR`. It is removed in a `finally` on every path,
   including the cancellation of a replaced flow and daemon shutdown; the PTY's process group is killed first.
5. Allow-list environment built from scratch: `PATH` (the directory of the resolved login program plus
   `/usr/bin:/bin:/usr/sbin:/sbin`), `HOME`, `CLAUDE_CONFIG_DIR`, `TMPDIR`, `BROWSER` (capture helper), `TERM`,
   `LANG`, `COLUMNS`, `NO_COLOR`. Nothing of the runner's environment is inherited (no `SSL_CERT_FILE`,
   `NODE_EXTRA_CA_CERTS`, proxy or keychain variables), so no existing profile can be reached. The program is resolved
   with the runner's `PATH` before the start.
6. The authorize URL is the code page link read from the PTY screen, the rows below "Browser didn't open? Use the URL
   below to sign in" (`setup_token.URL_LABEL`, `full_login.screen_url`), once the input field is drawn below it; the URL
   the program hands to the `BROWSER` capture helper redirects to `http://localhost:<port>/callback` and is not relayed,
   the helper keeps the device from opening a browser and writes that URL to `url.txt`, which the relay does not read;
   the link must fit the response contract (https, or http on loopback for doubles). The PTY is 1000 columns wide; the
   token is read from the replayed terminal screen (`login/screen.py`; the control sequences are not stripped): the
   first run of non-empty rows after the row containing `OAuth token`, joined (hard-wrapped token),
   format `sk-ant-oatNN-[A-Za-z0-9_-]{16,4000}`. The master side is read until EOF before the child is reaped (macOS
   drops unread output otherwise).
7. Time limits and codes: no URL within 30 s → `timeout`; the command exits before a URL, cannot be started or hands
   over an unusable URL → `login_cli_failed`; waiting for the code ends 5 s before the request expires → `timeout`;
   after the code the command must end within 60 s → `timeout`; non-zero exit with `OAuth error` → `code_invalid`,
   other non-zero exit → `login_cli_failed`; exit 0 without a valid token → `token_invalid`. All limits are fields of
   `LoginOptions` (tests set `context.login_options`).
8. The stored body is `{token, email, kind:"setup_token"}` (credential contract), written with
   `credentials.cache.put` under the newest `CK` epoch while the root request is open. Store failures: no `CK` →
   `credential_missing`, a hub refusal → `upstream_error`, the hub unreachable → `upstream_unreachable`.
9. `email`: the real CLI and the double print none, so the relay stores `null` and answers `{step:"done"}`. After a
   successful automation the address the browser signed in with (the `mailbox` credential's `email`) is stored and
   answered — it is a fact the runner observed, not an invention (06-web.md §8.2 shows the subscription e-mail). The
   `mailbox` body is checked against the patterns of its contract (host, address ≤ 254 with the shared e-mail
   pattern), so the stored record and the response always fit their contracts.

## Automation (`automation.py`)
10. Mode `automatic` uses the automation only when `LOGIN_AUTOMATION` is on and Playwright can be imported; otherwise
    (org device default, explicit `off`, extra missing) the relay starts at once with
    `{step:"authorize", url, automation:"failed"}` and the reason (`disabled`, `extra_missing`) is logged. With the
    automation possible, a missing or malformed `mailbox` credential is `credential_missing` before the CLI starts
    (contract description of `credential_missing`).
11. Pages are recognised by `body[data-page=…]` and element IDs of the auth-page double (DBL-03); the automation is
    only exercised hermetically (PROJECT.md: no live login). Every page step waits at most `page_timeout` (30 s);
    any other page (human-verification, error page), browser error, missing mail within `mail_wait` (10 min, polled
    every 5 s with the unchanged legacy `newest_magic_link`) or a code of the wrong form falls back to the relay.
    Progress responses `browser_started`, `email_entered`, `magic_link_received`, `authorized` are sent on the way.
12. Magic-link pattern: `<origin of the authorize URL>/magic-link#<token>`; sender filter `anthropic.com` (IMAP
    `SEARCH FROM` substring). The auth-page double is started with a matching sender in the tests; SCN-05 must do the
    same (`--mail-from`).
13. Trust: Chromium uses its own trust store. When `SSL_CERT_FILE` is set (private CA, also used by the legacy IMAP
    poll), the authorize origin's certificate is verified against it with Python's TLS (hostname and chain) and
    Chromium gets exactly that server key as `--ignore-certificate-errors-spki-list` (no blanket ignoring of
    certificate errors). The browser runs with a persistent profile in `<temp dir>/browser` (sign-in cookies), so it is
    removed with the temporary directory; Chromium comes from `PLAYWRIGHT_BROWSERS_PATH` or, when unset, from
    `<runner home>/ms-playwright` installed by the platform profile.
14. A `RequestClosed` raised by a progress response is not turned into a fall-back: the hub no longer accepts
    answers, the flow ends (RUN-09 frame) and the directory is removed.

## Checks
15. `credential.check` uses `POST <MODEL_UPSTREAM_ANTHROPIC>/v1/messages/count_tokens` for both kinds: `x-api-key` for
    `anthropic_api_key`, `authorization: Bearer` + `anthropic-beta: oauth-2025-04-20` for `claude_subscription`.
    `/api/hello` is unauthenticated (Claude Code's connectivity probe; the model double checks no credential there),
    so it cannot validate a token. 2xx → `valid:true`, 401/403 → `valid:false`, other status → `upstream_error`,
    network error → `upstream_unreachable`. The probe model is the catalog's default for the access kind, else
    `claude-haiku-4-5`; `count_tokens` consumes no quota.
16. `mailbox.check`: IMAPS (`IMAP4_SSL`, default context, 30 s) login and `LIST`; a refused login is `valid:false`, a
    connection/TLS error `upstream_unreachable`, a failed `LIST` `upstream_error`; the connection is always logged out.

## Install profile
17. `agent-runner service install --profile platform|org` (05-runner.md §1.3, package scope) is a three-line hook in
    `cli.py` (owned by RUN-01) calling `agent_runner/login/profile.py:apply`; the parser had no extension point for an
    option of an existing command. `platform` installs the extra when missing (`<python> -m pip install
    playwright>=1.65`) and always (idempotent) Chromium into `<runner home>/ms-playwright` (`/usr/bin/env
    PLAYWRIGHT_BROWSERS_PATH=… <python> -m playwright install chromium`), both through `sudo -u <runner user> -H` when
    the caller is root, so the daemon account owns and finds them; each step may take 30 min; a failure is
    `login_extra_install_failed`. It writes `LOGIN_AUTOMATION=on` into the plist environment; `org` writes `off`; no profile keeps the device-kind default of
    `load_config`. An explicit `AGENT_RUNNER_LOGIN_AUTOMATION` wins. Under pytest no installer runs.

## Tests
18. The doubles bind ephemeral loopback ports chosen by the kernel (as the DBL-03 suites do); they never collide with
    other packages' fixed ranges.
19. The automation suites need Playwright for Python and Chromium (`PLAYWRIGHT_BROWSERS_PATH`); a missing extra is a
    test failure (`pytest.fail`), not a skip. The runner test job of `.github/workflows/platform-runner.yml` therefore
    installs `[platform,platform-test,login]` and `playwright install chromium` (`--with-deps` on Linux) — a two-line
    change in a shared file, so the workflow keeps running the same commands as the local gates (ADR-027 §6).
    `tests/platform/runner/requests/test_service.py` (RUN-09) expected only `github.connect` as discovered handler
    ("until RUN-13", RUN-09 decision 7); it now lists the four kinds.
20. All response bodies of every test are validated against `contracts/requests/<kind>.response.json`, stored
    subscriptions against `contracts/credentials/claude_subscription.json`; every test checks that no `login-*`
    directory is left.

## Review (independent subagent)
21. No high findings. Medium, fixed: the platform profile installed Chromium for root instead of the daemon account
    (now `sudo -u`, `<runner home>/ms-playwright`, 30 min limit, Chromium checked on every install); the GitHub
    workflow lacked the `login` extra (now installed, 19). Low, fixed: `killpg` after the child was reaped (skipped
    now), synchronous `reap` on the event loop (now in a thread), typing into a closed terminal (`login_cli_failed`),
    silent incomplete cleanup (logged `cleanup_incomplete`), unvalidated mailbox `email`/host (contract patterns), the
    browser profile outside the temporary directory (persistent profile inside), the secret scan of the automation
    test (now also code, state and magic-link token). Accepted with reasons: the `cli.py` hook (17); the page markers
    of the double (11: the automation is hermetic by rule and falls back on real pages); the keychain isolation of the
    real CLI relies on `CLAUDE_CONFIG_DIR` and the fresh `HOME` — the runner never touches the keychain itself and
    the LIVE tests never run a login (ADR-011); the substring sender filter is the legacy behaviour, bounded by the
    origin-bound link pattern; `PATH` holds system directories (5), which is required to start any program.
