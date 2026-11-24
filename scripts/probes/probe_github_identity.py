"""Probe of GitHub identities for the operator (KOR-47 stage a, ADR-076; `probelib.github_identity`).

Never part of `make platform-ci`; run once after the merge, on any machine with network access to GitHub, against a
test repository of a test account:

    python scripts/probes/probe_github_identity.py --repo OWNER/NAME --token-file FILE \
        --co-author LOGIN:ID [--api URL] [--keep-ref]

`--token-file` holds the token (one line, mode 0600; it is never printed). For question 3 the token needs the classic
scope `write:ssh_signing_key` or `admin:ssh_signing_key` (only the latter lets the probe delete its key again).
`--co-author` names a second GitHub account (login and numeric ID) used as the author of the attribution commit and as
a co-author. The result is printed as JSON; exit code 0 when every request the probe needed succeeded, 1 otherwise, 2
for invalid arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probelib.github_identity import Account, http_sender, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--co-author", required=True, help="LOGIN:ID of a second account")
    parser.add_argument("--api", default="https://api.github.com")
    parser.add_argument("--keep-ref", action="store_true")
    args = parser.parse_args(argv)
    login, _, number = str(args.co_author).partition(":")
    if not login or not number.isdigit():
        parser.error("--co-author must be LOGIN:ID")
    token = args.token_file.read_text(encoding="ascii").strip()
    try:
        report = run(
            http_sender(args.api, token), args.repo, co_author=Account(login, int(number)), keep_ref=args.keep_ref
        )
    except RuntimeError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(report.json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
