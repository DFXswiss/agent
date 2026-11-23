# DBL-03 acceptance (mail double, login-CLI double, auth-page double)

| Criterion | Evidence |
|---|---|
| SMTP → IMAP delivery | `tests/platform/doubles/mail/test_smtp.py` (smtplib delivery, dot-unstuffing, bare newlines, size limit, refused recipients, protocol errors), `test_imap.py` (LOGIN, LIST, SELECT/EXAMINE, SEARCH keys, FETCH items and partials, literals, new-mail announcements) |
| Junk placement | `test_store.py::test_delivery_rules` (`X-Spam-Flag`, junk senders, explicit folder), `test_legacy.py::test_magic_link_in_junk`, `login_cli/test_authpage.py::test_full_flow_with_the_link_in_junk` |
| IMAPS with a runtime test CA | `test_testca.py` (strict verification, `SSL_CERT_FILE`, foreign CA refused, SPKI pin against `openssl`, key modes, reuse), `test_imap.py::test_imaplib_flow_over_imaps`; no key in the repository (hygiene scan) |
| Legacy compatibility | `test_legacy.py`: `agent_cli.account_login.newest_magic_link` unchanged (default `IMAP4_SSL` and context) finds the link in Junk, picks the newest across folders, filters sender and age, finds a spam folder by name, raises on wrong credentials and refuses an untrusted CA |
| CLI flows (success, timeout, wrong code) | `login_cli/test_cli.py`: real PTY runs with a `BROWSER` capture helper (success with token block, wrong code, `timeout` and `no-url` variants, no browser) and in-process branches (input errors, read timeout, exchange failures, usage) |
| Auth pages with a headless browser | `login_cli/test_browser.py`: Chromium (Playwright for Python, `login` extra) drives e-mail form → magic link from Junk via the legacy poll → Authorize → code → PTY → token; an injected unexpected page is visible; HTTP-level coverage of every page, error and fault in `test_authpage.py` |
| Deterministic tokens and codes | `login_cli/test_codes.py`; tests assert the exact n-th code and token of a seed |
| `python -m` entry points | `mail/test_cli.py`, `login_cli/test_authpage_cli.py` (in process and as subprocesses until SIGTERM), `login_cli/test_cli.py` (PTY) |
| Gates (100 %, ruff, mypy --strict, stdlib only) | `tools/wp/gates.sh DBL-03`; `agent_doubles.mail` and `agent_doubles.login_cli` 100 % lines and branches; `test_packaged.py` in both suites |
