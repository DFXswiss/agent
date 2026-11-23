"""The `BROWSER` capture helper of the subscription login (05-runner.md section 11.2 step 2).

`claude setup-token` opens its authorize URL with `$BROWSER <url>`. The runner points `BROWSER` at a tiny shell script
in the login's temporary directory that writes its first argument atomically to `url.txt` (write to a partial file,
then rename), so the URL never has to be scraped from the terminal UI, which wraps long lines. Pattern copied from
`agent_cli.account_login._write_capture_helper` (legacy, unchanged).
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

HELPER = "capture-url"
TARGET = "url.txt"
MAX_URL = 2048
URL_RE = re.compile(r"^(https://[!-~]{1,2040}|http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?(/[!-~]{0,2000})?)$")
"""The authorize URL of `contracts/requests/claude.login.response.json` (https, or http on loopback for doubles)."""


class UrlInvalid(Exception):
    """The captured text is not an authorize URL the response contract accepts."""


def write_helper(workdir: Path) -> Path:
    """An executable (0700) that writes its first argument to `workdir/url.txt`; its path."""
    target = workdir / TARGET
    partial = workdir / (TARGET + ".partial")
    helper = workdir / HELPER
    script = (
        "#!/bin/sh\n"
        f"printf '%s' \"$1\" > {shlex.quote(str(partial))} && "
        f"mv {shlex.quote(str(partial))} {shlex.quote(str(target))}\n"
    )
    descriptor = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(script)
    return helper


def captured(workdir: Path) -> str | None:
    """The captured URL once the helper ran, None before; `UrlInvalid` for anything that is not an authorize URL."""
    target = workdir / TARGET
    if not target.is_file():
        return None
    data = target.read_bytes()[: MAX_URL + 1]
    try:
        url = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise UrlInvalid from None
    if len(url) > MAX_URL or not URL_RE.fullmatch(url):
        raise UrlInvalid
    return url
