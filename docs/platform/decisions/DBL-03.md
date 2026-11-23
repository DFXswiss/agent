# DBL-03 decisions (mail double, login-CLI double, auth-page double)

Small decisions of the login doubles package (07-testing.md §9; 05-runner.md §1.3 `LOGIN_ARGV`, §11.2, §11.3;
ADR-011; conventions of `agent_doubles.common` from DBL-01).

## Mail double (`agent_doubles.mail`)
1. Layout: `store.py` (accounts, folders, messages, spam rules), `smtp.py` and `imap.py` on a small line server
   `tcp.py` (bound to one address, optional implicit TLS, closes open connections on stop), `record.py` (journal and
   failures), `testca.py` (runtime CA), `message.py` (composing test mails), `control.py` (control-route plumbing, also
   used by the auth-page double), `double.py` (`MailDouble`), `__main__.py`.
2. Four listeners on one address: control routes over HTTP (`url`, as for every double), SMTP, IMAP and IMAPS. The
   protocol ports are announced through `GET /_double/info` and `--info-file` (JSON), because `--port-file` of the
   common CLI holds one port only.
3. Spam placement without a filter: a message goes to the spam folder when the delivery names it, when it carries
   `X-Spam-Flag: YES` (the header spam filters add; the auth-page double sets it for a "junk" scenario) or when its
   envelope sender is listed with `--junk-sender`. This keeps the auth-page double a plain SMTP client.
4. The spam folder is `Junk` with the RFC 6154 `\Junk` attribute by default; an account may name it differently and
   leave the attribute out (e.g. `INBOX.Spam`, delimiter `.`), which exercises the name fallback of the legacy poll.
5. IMAP subset beyond the package text (LOGIN, LIST with `\Junk`, SELECT, SEARCH by sender/date, FETCH): also
   CAPABILITY, NOOP, LOGOUT, EXAMINE (read-only, used by the legacy poll), CLOSE, UID SEARCH/FETCH, SEARCH keys TO,
   SUBJECT, SEEN/UNSEEN, NOT, OR, UID and sequence sets, FETCH partials `<origin.count>` and client literals up to
   64 KiB. Not modelled: STORE, EXPUNGE, APPEND, STATUS, IDLE, STARTTLS, AUTHENTICATE, ENVELOPE/BODYSTRUCTURE,
   CHARSET in SEARCH. Messages are only appended, so sequence numbers and UIDs never shift; new mail is announced
   with `* n EXISTS` before the results of the next command.
6. Failures (`state.ok` false): `BAD` answers (unknown command, syntax, wrong state), SMTP `500`/`501`/`503`, a
   failed delivery after `RCPT`, any HTTP request outside `/_double/`. Not failures: wrong IMAP credentials (`NO`), an
   unknown mailbox (`NO`), an unknown SMTP recipient (`550`): these are legitimate test cases. `done` is always true
   (the double has no expectations).
7. Journal: one record per SMTP/IMAP command in the common format (`method` = protocol, `path` = verb); IMAP passwords
   are journaled as `<redacted n chars>`, a command that cannot be parsed is journaled without its text (it may hold a
   password).
8. `POST /_double/reset` empties the folders, the journal and the failures but keeps the accounts (they are
   configuration, given on the command line). UIDs continue after a reset; IMAP clients with a selected folder must
   reconnect (no EXPUNGE responses are sent). The mail double has no scenario, so it has no `/_double/scenario`
   route (`DoubleClient.scenario` answers 404 there).
8a. Accounts come from `--account USER:PASSWORD` or, to keep passwords out of the process list, `--accounts-file`
   (JSON list, same keys as `POST /_double/accounts`). The account's `repr` hides the password.

## Test CA (`agent_doubles.mail.testca`)
9. The standard library cannot create keys or certificates, so `DoubleCA` runs the `openssl` command (OpenSSL 3/4 and
   the LibreSSL of macOS both work; the config-file form avoids `-addext`). P-256 keys, SHA-256, two days of validity,
   the extensions `VERIFY_X509_STRICT` needs. Nothing is stored in the repository; keys are 0600 in a 0700 directory
   (temporary unless `--ca-dir` is given).
10. One server certificate for `localhost`, `127.0.0.1`, `::1`, the bound address and any `--ca-name`.
    `DoubleCA.open_or_create` reuses a CA directory so the mail and the auth-page double of one run share one CA (the
    legacy poll trusts the mail double through `SSL_CERT_FILE`, which holds a single CA file). It works under an
    exclusive `flock` on `<dir>/.lock` (parallel starts never interleave files), renews a CA whose server certificate
    expires within an hour (`openssl x509 -checkend`) and refuses a valid CA that lacks a requested name instead of
    replacing it under doubles already using it: the orchestrator creates the shared CA with every name first.
11. Browsers trust the auth pages through the SPKI pin (`spki_sha256`, Chromium
    `--ignore-certificate-errors-spki-list`), computed from the certificate DER with a minimal parser (no extra
    command). Recommendation for RUN-13: in test mode pass this pin to Chromium rather than ignoring certificate
    errors altogether.

## Login-CLI double (`agent_doubles.login_cli.cli`)
12. Output strings follow the installed Claude Code 2.1.330 (read from the binary's string table, no login run):
    `Browser didn't open? Use the URL below to sign in`, `Paste code here if prompted > `, `✓ Long-lived
    authentication token created`, `Your OAuth token (valid for 1 year):`, the token on its own line, `Store this
    token…`, `Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>`, errors as `OAuth error: …`.
    The pasted value is `CODE#STATE` as on the real code page.
13. The CLI exchanges the code at the auth-page double's token endpoint (PKCE S256 verifier, state, redirect URI) like
    the real CLI, so a wrong code is decided by the auth-page double. `--auth-url`, `--ca-file` and `--seed` are
    arguments rather than environment variables, because the runner starts `LOGIN_ARGV` with an allow-list
    environment. The positional `setup-token` is optional, so `LOGIN_ARGV` can read
    `["python", "-m", "agent_doubles.login_cli", "setup-token", "--auth-url", …]`.
14. Variants: `ok`, `no-url` (no browser call, no URL; for the runner's 30 s URL limit) and `timeout` (URL printed,
    input ignored). A wrong code needs no variant. `--code-timeout` (default 600 s) bounds every wait.
14a. The real CLI is an Ink terminal UI that hard-wraps long words at the terminal width and colours its output; the
    double prints plain lines by default and `--columns N` hard-wraps the URL and the token at N characters. RUN-13
    takes the URL from the `BROWSER` capture helper, runs the PTY wide, strips ANSI sequences and joins wrapped token
    lines (tests with `--columns`). The real `setup-token` prints no e-mail address, so the double prints none either;
    the optional `email` of 05 §11.2 step 5 stays absent with both.
15. `$BROWSER <url>` is started with a 10 s limit and any failure is ignored, as the URL is printed anyway. With
    `CLAUDE_CONFIG_DIR` set the CLI writes `.claude.json` there, so RUN-13 can prove that its temporary configuration
    directory is removed.
16. Token shape `sk-ant-oat01-` + 93 base64url characters + `AA` (`codes.TOKEN_PATTERN`); every value derives from a
    seed by HMAC (`codes`), so tests predict the n-th code and token. The token prefix is assembled at run time so no
    source file holds a token-shaped literal (hygiene scan).
17. Observation for RUN-13: on macOS, PTY output that is not read before the child exits is lost when the last slave
    descriptor closes; the relay must drain the master until EOF before reaping the process (the test helper does).

## Auth-page double (`agent_doubles.login_cli.authpage`)
18. Pages over HTTPS (shared `DoubleCA`) because the legacy poll accepts only `https://` magic links; plain HTTP is
    possible for HTTP-level tests but then the legacy poll ignores the link (tested). Control routes run on a separate
    plain-HTTP listener so `DoubleClient` and the orchestrator treat it like every other double.
19. Each page carries `data-page` (`email`, `check-email`, `challenge`, `magic-link`, `authorize`, `code`, `error`) and
    stable element ids (`#email`, `#continue`, `#authorize`, `#decline`, `#authorization-code`, `#error-code`) so a
    browser driver can detect unexpected states without parsing text. The magic link is `<base>/magic-link#<token>`;
    the page's script posts the fragment, so links never reach server logs.
20. Faults for the automation fallback (05 §11.3): `email_page` (human verification instead of the mail), `no_mail`,
    `authorize_page`, `code_page` (error pages) and `token` (500 at the exchange). Milestones (`email_page`,
    `mail_sent`, `signed_in`, `authorize_page`, `authorized`, `declined`, `code_page`, `token`) and the scenario's
    `expect` list define `done`.
21. Failures: invalid OAuth parameters, malformed token requests, unknown pages, unreadable bodies and a mail that
    cannot be sent (no SMTP, refused recipient). A wrong or reused code, an expired or used link, a decline and a
    missing session are legitimate test cases and answered with error pages or `invalid_grant`. `/favicon.ico`
    answers 404 without a failure (browsers request it on their own).
22. Session cookie `double_session` (`HttpOnly`, `SameSite=Lax`, `Secure` over TLS); cookies are redacted in the
    journal by the common journal rules. Magic links live 15 min, codes 10 min (both single use).
22a. The journal never holds a value that grants access: `token`, `code`, `code_verifier`, `state`, `access_token` and
    `next` (it carries the state) are replaced by `<redacted n chars>` in queries and bodies. Error codes and all page
    values are HTML-escaped; a callback `error` other than `access_denied` is shown as `invalid_request`. The token
    endpoint checks the PKCE verifier's charset (RFC 7636). The page server sets a 300 s socket timeout; SMTP uses
    `local_hostname="localhost"` (no FQDN lookup).

## Tests
23. The legacy `agent_cli.account_login.newest_magic_link` is called unchanged with its default `imaplib.IMAP4_SSL`
    and `ssl.create_default_context()`; only `SSL_CERT_FILE` points at the runtime CA.
24. The headless smoke (`test_browser.py`) uses Playwright for Python from the `login` extra and is skipped with a
    reason when the extra is missing; the same chain runs on the HTTP level in `test_cli.py` and `test_authpage.py`.

## Review
25. Independent review (fresh subagent): no high findings. Medium, all fixed: unescaped error code on the error page,
    login secrets in the auth-page journal, unchecked CA reuse (names, expiry, lock), plain output versus the wrapping
    terminal UI (`--columns`, 14a). Low, fixed: SMTP FQDN lookup, password in `Account.repr`, passwords only via argv
    (`--accounts-file`), reset under a live selection (documented, 8), unbounded sequence ranges (cut at the largest
    number), non-ASCII verifier, page-server timeout, untyped account input, test capture helper now atomic like the
    legacy helper, poll loop with a pause, unused parameter. Rejected: printing an e-mail address from the CLI double
    (the real command prints none, 14a).
