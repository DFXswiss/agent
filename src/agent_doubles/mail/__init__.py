"""Mail double (docs/platform/spec/07-testing.md section 9): SMTP in, IMAP and IMAPS out with INBOX and a ``\\Junk``
folder, a test CA created at run time, a journal and control routes; standard library only (keys come from the
``openssl`` command).

- `store`: accounts, folders, messages and the spam rules.
- `smtp`, `imap`: the protocol servers (on `tcp`); `record`: journal and failures.
- `testca`: the runtime CA; `message`: composing test mails.
- `double`: `MailDouble` with its control routes (`control`); ``python -m agent_doubles.mail`` runs it.

The legacy IMAP poll `agent_cli.account_login.newest_magic_link` works unchanged against it.
"""
