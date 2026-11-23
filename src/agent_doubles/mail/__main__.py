"""Run the mail double from the command line.

    python -m agent_doubles.mail --account user@example.test:password --info-file /tmp/mail.json --port-file /tmp/p

``--account USER:PASSWORD`` (repeatable; the password is everything after the first colon) creates accounts with an
INBOX and a ``Junk`` folder (``\\Junk``); ``--accounts-file`` reads a JSON list of accounts instead (keeps passwords
out of the process list; keys as for ``POST /_double/accounts``).

- ``--smtp-port``, ``--imap-port``, ``--imaps-port``: protocol ports (0: free ports).
- ``--no-tls``: no IMAPS listener and no CA.
- ``--ca-dir``: keep the runtime CA in a directory and reuse a CA already there (default: a temporary one, removed at
  exit); ``--ca-name`` adds certificate names (for doubles that share the CA but are reached under another address).
- ``--junk-sender``: deliver mail of this envelope sender into the spam folder.
- ``--info-file``: receives the endpoints as JSON (``smtp``, ``imap``, ``imaps``, ``ca_file``, ``spki_sha256``,
  ``control``).

Common options and exit codes as for every double (`agent_doubles.common.cli`); ``--port`` and ``--port-file`` refer
to the control server.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event

from agent_doubles.common.cli import EXIT_USAGE, add_server_arguments, poll_wait, serve_until_signal
from agent_doubles.mail.double import MailDouble
from agent_doubles.mail.store import StoreError, parse_account
from agent_doubles.mail.testca import DoubleCAError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_doubles.mail", description="Mail double (SMTP, IMAP, IMAPS)")
    add_server_arguments(parser)
    parser.add_argument("--account", action="append", default=[], help="USER:PASSWORD; repeatable")
    parser.add_argument(
        "--accounts-file",
        type=Path,
        help="JSON list of accounts (user, password, junk_name, junk_attribute, delimiter)",
    )
    parser.add_argument("--ca-name", action="append", default=[], help="further certificate name; repeatable")
    parser.add_argument("--smtp-port", type=int, default=0)
    parser.add_argument("--imap-port", type=int, default=0)
    parser.add_argument("--imaps-port", type=int, default=0)
    parser.add_argument("--no-tls", action="store_true", help="no IMAPS listener and no CA")
    parser.add_argument(
        "--ca-dir", type=Path, help="directory of the runtime CA (reused when present, kept after exit)"
    )
    parser.add_argument("--junk-sender", action="append", default=[], help="envelope sender delivered as spam")
    parser.add_argument("--info-file", type=Path, help="write the endpoints as JSON to this file")
    return parser


def main(argv: Sequence[str] | None = None, *, wait: Callable[[Event], None] = poll_wait) -> int:
    args = build_parser().parse_args(argv)
    try:
        double = MailDouble(
            host=args.host,
            port=args.port,
            smtp_port=args.smtp_port,
            imap_port=args.imap_port,
            imaps_port=args.imaps_port,
            tls=not args.no_tls,
            ca_dir=args.ca_dir,
            journal_path=args.journal,
            info_path=args.info_file,
            junk_senders=args.junk_sender,
            ca_names=args.ca_name,
        )
    except (OSError, ValueError, DoubleCAError) as exc:
        print(f"mail double: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        for account in args.account:
            user, separator, password = account.partition(":")
            if not separator:
                raise StoreError("--account needs USER:PASSWORD")
            double.add_account(user, password)
        if args.accounts_file is not None:
            listed = json.loads(args.accounts_file.read_text(encoding="utf-8"))
            if not isinstance(listed, list):
                raise StoreError("--accounts-file must hold a JSON list")
            for item in listed:
                user, password, options = parse_account(item)
                double.add_account(user, password, **options)
    except (StoreError, OSError, ValueError) as exc:
        double.stop()
        print(f"mail double: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return serve_until_signal(double, args.port_file, wait)


if __name__ == "__main__":
    sys.exit(main())
