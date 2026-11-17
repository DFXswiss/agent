"""Sign one subscription into the profile of a configured account.

``agent account-login`` does every step a script may do and leaves the clicks
in the browser to the human: sign-in pages may block automated browsers, so the
pages are opened in the user's normal browser through the operating system.
Optionally it reads the sign-in e-mail over IMAP and opens the sign-in link
itself. Every driver specific (the login and status commands, the sign-out
page, the authorize URL marker, the sign-in link pattern and the sender) comes
from the driver's ``login`` section in ``drivers.json``. The sign-in link,
tokens and the mailbox password are never printed, logged or stored.
"""

from __future__ import annotations

import imaplib
import json
import os
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timezone
from email import message_from_bytes
from email import policy as email_policy
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .ai_accounts import AIAccount
from .drivers import DRIVERS_DOC, DRIVERS_FILE, DriverSpec, LoginSpec, expand_template
from .store import StoreError
from .token_keepalive import driver_env

MAIL_WAIT_SECONDS = 600
FINISH_WAIT_SECONDS = 600
URL_WAIT_SECONDS = 30
MAIL_POLL_SECONDS = 5
LOGOUT_WAIT_SECONDS = 5
# Messages may carry a Date a little before the moment the page was opened.
DATE_SLACK_SECONDS = 5
MAX_MESSAGES = 10
STATUS_TIMEOUT_SECONDS = 60
MAILBOX_DIR = "account-login"
OUTPUT_PREFIX = "account.login"
_EMAIL = re.compile(r"[^@\s/\\]+@[^@\s/\\]+")
_MAILBOX_KEYS = frozenset({"imap_host", "imap_user", "imap_password"})
_MAILBOX_OPTIONAL = frozenset({"imap_port"})


@dataclass(frozen=True)
class Mailbox:
    host: str
    user: str
    password: str
    port: int = 993

    def __repr__(self) -> str:
        # The password never appears in a representation.
        return f"Mailbox(host={self.host!r}, user={self.user!r}, port={self.port})"


def _check_email(address: str) -> str:
    if not isinstance(address, str) or address.count("@") != 1 or not _EMAIL.fullmatch(address):
        raise StoreError("the e-mail address must contain exactly one @ and no spaces or slashes")
    return address


def mailbox_path(agent_home: Path, email: str) -> Path:
    """``$AGENT_HOME/account-login/<lowercased email>.json``."""
    return agent_home / MAILBOX_DIR / f"{_check_email(email).lower()}.json"


def load_mailbox(agent_home: Path, email: str) -> Mailbox | None:
    """The IMAP mailbox for ``email``; ``None`` when no file exists.

    The file must grant no group or other permission and must be a JSON object
    with exactly ``imap_host``, ``imap_user``, ``imap_password`` and an optional
    ``imap_port`` (1..65535). Errors never contain the password.
    """
    path = mailbox_path(agent_home, email)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError(f"cannot read {path}: {exc.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise StoreError(f"{path} is not a regular file")
    if info.st_mode & 0o077:
        raise StoreError(
            f"{path} grants group or other permissions; run chmod 600 {shlex.quote(str(path))}"
        )
    data: Any = None
    failure: str | None = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        failure = f"cannot read {path}"
    except (UnicodeError, ValueError):
        # Raised below: the decode error holds the file text, password included.
        failure = f"{path} is not valid JSON"
    if failure is not None:
        raise StoreError(failure)
    if not isinstance(data, dict):
        raise StoreError(f"{path} must be a JSON object")
    keys = set(data)
    if not _MAILBOX_KEYS <= keys or keys - _MAILBOX_KEYS - _MAILBOX_OPTIONAL:
        raise StoreError(
            f"{path} must hold exactly imap_host, imap_user, imap_password "
            "and an optional imap_port"
        )
    for key in sorted(_MAILBOX_KEYS):
        value = data[key]
        if not isinstance(value, str) or value.strip() == "":
            raise StoreError(f"{path}: {key} must be a non-empty string")
    port = 993
    if "imap_port" in data:
        port = data["imap_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise StoreError(f"{path}: imap_port must be an integer from 1 to 65535")
    return Mailbox(data["imap_host"], data["imap_user"], data["imap_password"], port)


def _message_bytes(fetched: Any) -> bytes | None:
    if not isinstance(fetched, list):
        return None
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _message_time(message: Message) -> float | None:
    header = message.get("Date")
    if header is None:
        return None
    try:
        when = parsedate_to_datetime(str(header))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _link_in(message: Message, pattern: re.Pattern[str]) -> str | None:
    """The first ``https://`` match of ``pattern`` in a text part of ``message``."""
    for part in message.walk():
        if part.get_content_type() not in ("text/html", "text/plain"):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        # The link is plain ASCII, so a byte-for-byte decode finds it in any
        # ASCII-compatible charset without trusting the declared one.
        for match in pattern.finditer(payload.decode("latin-1")):
            link = match.group(0)
            if link.startswith("https://"):
                return link
    return None


def newest_magic_link(
    mailbox: Mailbox,
    since: float,
    *,
    pattern: re.Pattern[str],
    mail_from: str,
    imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
) -> str | None:
    """The sign-in link of the newest matching e-mail sent after ``since``.

    IMAPS with certificate verification; INBOX is selected read-only. At most
    the newest ``MAX_MESSAGES`` messages whose sender matches ``mail_from`` are
    read, newest first, stopping at the first one dated before ``since`` minus
    a few seconds. Only an ``https://`` match of ``pattern`` counts. The
    connection is always logged out.
    """
    conn = imap_factory(
        mailbox.host, mailbox.port, ssl_context=ssl.create_default_context()
    )
    try:
        conn.login(mailbox.user, mailbox.password)
        status, _ = conn.select("INBOX", readonly=True)
        if status != "OK":
            raise StoreError("cannot open INBOX")
        status, found = conn.search(None, "FROM", f'"{mail_from}"')
        if status != "OK":
            raise StoreError("mailbox search failed")
        ids = found[0].split() if found and isinstance(found[0], bytes) else []
        for message_id in reversed(ids[-MAX_MESSAGES:]):
            status, fetched = conn.fetch(message_id, "(BODY.PEEK[])")
            if status != "OK":
                raise StoreError("mailbox fetch failed")
            raw = _message_bytes(fetched)
            if raw is None:
                continue
            message = message_from_bytes(raw, policy=email_policy.compat32)
            sent = _message_time(message)
            if sent is not None and sent < since - DATE_SLACK_SECONDS:
                return None
            link = _link_in(message, pattern)
            if link is not None:
                return link
        return None
    finally:
        conn.logout()


def _open_url(url: str, run: Callable[..., Any]) -> None:
    """Open ``url`` in the user's normal browser through the operating system."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    failure: str | None = None
    try:
        completed = run(
            [opener, url],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # The URL may be a sign-in link; the exception text is not kept.
        failure = f"{opener} failed ({type(exc).__name__})"
    if failure is not None:
        raise StoreError(failure)
    if completed.returncode != 0:
        raise StoreError(f"{opener} failed (exit {completed.returncode})")


def _copy_to_clipboard(text: str, run: Callable[..., Any]) -> None:
    """Put ``text`` on the macOS clipboard and verify it with ``pbpaste``."""
    if sys.platform != "darwin":
        raise StoreError("the clipboard is only filled on macOS")
    try:
        copied = run(
            ["pbcopy"], input=text, capture_output=True, text=True, timeout=10, check=False
        )
        pasted = run(
            ["pbpaste"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreError(f"clipboard failed ({type(exc).__name__})") from None
    if copied.returncode != 0 or pasted.returncode != 0 or pasted.stdout != text:
        raise StoreError("the clipboard does not hold the e-mail address")


def _write_capture_helper(workdir: Path) -> Path:
    """An executable that writes its first argument to ``workdir/url.txt``."""
    target = workdir / "url.txt"
    partial = workdir / "url.txt.partial"
    helper = workdir / "capture-url"
    helper.write_text(
        "#!/bin/sh\n"
        f"printf '%s' \"$1\" > {shlex.quote(str(partial))} && "
        f"mv {shlex.quote(str(partial))} {shlex.quote(str(target))}\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    return helper


def _drain(fd: int) -> None:
    """Read and discard the terminal output of the login command."""
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return
        if not chunk:
            return


def _stop(process: Any, sleep: Callable[[float], None]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    for _ in range(50):
        if process.poll() is not None:
            return
        sleep(0.1)
    process.kill()


def _wait_for_url(
    workdir: Path,
    process: Any,
    marker: str,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> str:
    target = workdir / "url.txt"
    started = clock()
    while True:
        if target.is_file():
            url = target.read_text(encoding="utf-8").strip()
            if not url.startswith("https://") or marker not in url:
                raise StoreError("unexpected authorize URL")
            return url
        code = process.poll()
        if code is not None:
            raise StoreError(f"login command exited before it opened a page (exit {code})")
        if clock() - started >= URL_WAIT_SECONDS:
            raise StoreError(f"login command opened no page within {URL_WAIT_SECONDS} s")
        sleep(0.2)


def _poll_mailbox(
    mailbox: Mailbox,
    since: float,
    process: Any,
    spec: LoginSpec,
    *,
    imap_factory: Callable[..., Any],
    open_url: Callable[[str], None],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    out: Callable[[str], None],
    email_address: str,
) -> bool:
    """Open the sign-in link once it arrives; ``True`` when it was opened."""
    while process.poll() is None and clock() - since < MAIL_WAIT_SECONDS:
        link: str | None = None
        failure: str | None = None
        try:
            link = newest_magic_link(
                mailbox,
                since,
                pattern=spec.magic_link_pattern,
                mail_from=spec.mail_from,
                imap_factory=imap_factory,
            )
        except (imaplib.IMAP4.error, OSError, StoreError) as exc:
            # Only the type: server text may echo the login.
            failure = type(exc).__name__
        if failure is not None:
            out(f"note: mailbox check failed ({failure}); retrying")
        elif link is not None:
            open_url(link)
            out(f'ACTION: finish the sign-in in the browser and choose "Authorize" ({email_address})')
            return True
        sleep(MAIL_POLL_SECONDS)
    return False


def _check_status(
    argv: list[str],
    spec: LoginSpec,
    env: Mapping[str, str],
    email_address: str,
    run: Callable[..., Any],
) -> str | None:
    """Verify the profile's login; return its subscription, if configured."""
    failure: str | None = None
    try:
        completed = run(
            argv,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure = f"status command failed ({type(exc).__name__})"
    if failure is not None:
        raise StoreError(failure)
    data: Any = None
    valid = True
    try:
        data = json.loads(completed.stdout)
    except (TypeError, ValueError):
        valid = False
    if not valid or not isinstance(data, dict):
        raise StoreError(
            f"status command returned no JSON object (exit {completed.returncode})"
        )
    found = data.get(spec.email_field)
    logged_in = data.get(spec.logged_in_field) is True
    if (
        not logged_in
        or not isinstance(found, str)
        or found.casefold() != email_address.casefold()
    ):
        shown = found if logged_in and isinstance(found, str) and found else "nobody"
        raise StoreError(f"signed in as {shown}, expected {email_address}")
    if spec.subscription_field is None:
        return None
    subscription = data.get(spec.subscription_field)
    if isinstance(subscription, str) and subscription.strip():
        return subscription
    return "subscription not reported"


def login_spec(account: AIAccount, drivers: Mapping[str, DriverSpec]) -> tuple[DriverSpec, LoginSpec]:
    """The account's driver and its ``login`` section, or a configuration error."""
    spec = drivers.get(account.provider)
    if spec is None or spec.login is None:
        raise StoreError(
            f"account {account.name} uses driver {account.provider}, which has no "
            f"login section in {DRIVERS_FILE} ({DRIVERS_DOC})"
        )
    return spec, spec.login


def _resolve(argv: list[str], which: Callable[[str], str | None]) -> list[str]:
    binary = which(argv[0])
    if binary is None:
        raise StoreError(f"{argv[0]} executable not found on PATH")
    return [binary, *argv[1:]]


def login(
    account: AIAccount,
    email: str,
    *,
    drivers: Mapping[str, DriverSpec],
    agent_home: Path,
    use_mailbox: bool = True,
    popen: Callable[..., Any] = subprocess.Popen,
    run: Callable[..., Any] = subprocess.run,
    opener: Callable[[str], None] | None = None,
    clipboard: Callable[[str], None] | None = None,
    imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Sign ``email`` into the profile of ``account``.

    Raises ``StoreError`` on every failure. The final status check must report
    exactly ``email``.
    """
    spec, login_section = login_spec(account, drivers)
    email_address = _check_email(email)
    mailbox: Mailbox | None = None
    if use_mailbox:
        mailbox = load_mailbox(agent_home, email_address)
        if mailbox is None:
            out(
                f"note: no mailbox file {mailbox_path(agent_home, email_address)}; "
                "continuing without mailbox"
            )
    login_argv = _resolve(
        expand_template(login_section.argv, {"email": email_address}), which
    )
    status_argv = _resolve(list(login_section.status_argv), which)
    base = os.environ if environ is None else environ
    profile_env = driver_env(spec, drivers, account.config_dir, base)
    open_url = opener if opener is not None else (lambda url: _open_url(url, run))
    copy = clipboard if clipboard is not None else (lambda text: _copy_to_clipboard(text, run))

    workdir = Path(tempfile.mkdtemp(prefix="agent-account-login-"))
    process: Any = None
    master: int | None = None
    drain: threading.Thread | None = None
    try:
        helper = _write_capture_helper(workdir)
        env = dict(profile_env)
        env["BROWSER"] = str(helper)
        master, slave = os.openpty()
        failure: str | None = None
        try:
            process = popen(
                login_argv,
                env=env,
                cwd=str(workdir),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            failure = f"cannot start {login_argv[0]}: {exc}"
        finally:
            os.close(slave)
        if failure is not None:
            raise StoreError(failure)
        drain = threading.Thread(target=_drain, args=(master,), daemon=True)
        drain.start()

        url = _wait_for_url(
            workdir, process, login_section.authorize_url_marker, clock=clock, sleep=sleep
        )
        try:
            copy(email_address)
        except StoreError as exc:
            out(f"note: {exc}; type the e-mail address in the browser")
        else:
            out(f"note: {email_address} is on the clipboard")

        # Sign the site out first so the page cannot belong to another account
        # that is still signed in; the final status check stays the safeguard.
        open_url(login_section.logout_url)
        sleep(LOGOUT_WAIT_SECONDS)
        since = clock()
        open_url(url)
        out(
            "ACTION: in the browser, paste the e-mail address if the field is empty "
            f'and choose "continue with e-mail" ({email_address})'
        )
        manual = (
            f"ACTION: open the sign-in link from the e-mail to {email_address}, "
            'then choose "Authorize"'
        )
        if mailbox is None:
            out(manual)
        elif not _poll_mailbox(
            mailbox,
            since,
            process,
            login_section,
            imap_factory=imap_factory,
            open_url=open_url,
            clock=clock,
            sleep=sleep,
            out=out,
            email_address=email_address,
        ):
            if process.poll() is None:
                out(f"note: no sign-in e-mail found within {MAIL_WAIT_SECONDS} s")
                out(manual)

        finish_started = clock()
        while process.poll() is None:
            if clock() - finish_started >= FINISH_WAIT_SECONDS:
                raise StoreError(
                    f"login command did not finish within {FINISH_WAIT_SECONDS} s"
                )
            sleep(1)
        if process.returncode != 0:
            raise StoreError(f"login command failed (exit {process.returncode})")

        subscription = _check_status(
            status_argv, login_section, profile_env, email_address, run
        )
        line = f"{OUTPUT_PREFIX} ok {account.name} {email_address}"
        if subscription is not None:
            line += f" ({subscription})"
        out(line)
    finally:
        if process is not None:
            _stop(process, sleep)
        if drain is not None:
            drain.join(timeout=1)
        if master is not None:
            os.close(master)
        shutil.rmtree(workdir)
