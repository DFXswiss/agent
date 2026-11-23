"""Operator side of the live tests (07-testing.md section 6; LVT-01): secrets in, artefacts checked, profile unchanged.

Runs on the operator's machine, never on the runner device and never in `make platform-ci`:

    python -m live.driver --result FILE --artifacts DIR [secret sources] [--fetch "command"] -- command...

1. Secrets are read only and only into memory (every source is optional; a test whose secret is missing fails or,
   LIVE-04, documents the skip):
   - OpenRouter platform key: `AGENT_LIVE_OPENROUTER_KEY`, else `--openrouter-service S` (`security
     find-generic-password -s S -w`, a read of the operator's keychain item);
   - Anthropic API key (LIVE-04): `AGENT_LIVE_ANTHROPIC_API_KEY`, else `--anthropic-service S`;
   - GitHub token (LIVE-02): `AGENT_LIVE_GITHUB_TOKEN`, else the standard output of `--github-token-command`
     (e.g. `gh auth token`: the token of the existing login, nothing is created);
   - Claude subscription access token (LIVE-03): with `--profile DIR` the agent repository's tested probe
     (`--probe .../scripts/probes/probe_live03.py`, `--probe-python`) checks the remaining validity, takes a snapshot
     of the profile (file digests and keychain attributes without the secret), and prints the **current access
     token** into a pipe - no refresh, login or logout, no keychain change, never the refresh token. Profiles the
     operator excluded (`--exclude-profile`, `AGENT_LIVE_EXCLUDED_PROFILES`) are refused here and by the probe.
     The only access to the profile is that read: a snapshot right after it must equal the first one (`read`;
     session files of other processes on the same profile - transcripts, shell snapshots - may change within these
     seconds: the operator names such path prefixes with `--volatile`, differences there are reported, every other
     file change and every keychain change fails). A third snapshot after the run must show the keychain item
     unchanged (`run`: no refresh, login or logout happened); file changes over the run are the other processes'
     session activity and are reported, not judged.
2. The command (the operator's transport to the runner device ending in `e2e/platform/run.sh --live --live-stdin`)
   gets the values on standard input only - `E2E_LIVE_<NAME>=<secret>` lines, then the operator's non-secret
   `AGENT_LIVE_*` settings (e.g. `AGENT_LIVE_GITHUB_REPO`) - never in an argument, a file or the environment.
   Its output goes to `--log`.
   With `--github-cleanup PREFIX` the configured repository must be private (checked before the run), and after the
   run every open pull request from a `PREFIX…` branch is closed and every such branch deleted - the fallback when the
   spec's own cleanup was cut off (timeout, interrupt); a failed step fails the driver.
   Child processes get the operator's environment without any `AGENT_LIVE_*`/`E2E_LIVE_*` variable.
3. Afterwards (also after a failure): the `--fetch` command (copies the run's artefacts to `--artifacts`), the secret
   scan of the artefacts and the log for every secret value (`orchestrator.artifacts.scan`, also inside zip files),
   the second profile snapshot and the comparison. `--result` gets a JSON summary without any secret value.

Exit 0 only when the command succeeded, no artefact holds a secret and the profile is unchanged; 1 otherwise; 2 for
invalid options or a missing source.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.artifacts import scan

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
SECURITY = "/usr/bin/security"
MIN_VALID_S = 7200  # the whole run (LIVE-03 runs after LIVE-01 and LIVE-02); the probe never refreshes
SECRET_ENV = {
    "AGENT_LIVE_OPENROUTER_KEY": "E2E_LIVE_OPENROUTER_KEY",
    "AGENT_LIVE_ANTHROPIC_API_KEY": "E2E_LIVE_ANTHROPIC_KEY",
    "AGENT_LIVE_GITHUB_TOKEN": "E2E_LIVE_GITHUB_TOKEN",
}
SUBSCRIPTION = "E2E_LIVE_SUBSCRIPTION_TOKEN"
FILE_CHANGES = frozenset({"changed", "added", "removed"})
EXCLUDED_ENV = "AGENT_LIVE_EXCLUDED_PROFILES"
# The operator's settings handed to the run (an allow-list: a misspelt secret variable is never forwarded).
SETTINGS = ("AGENT_LIVE_GITHUB_REPO",)
LIVE_PREFIXES = ("AGENT_LIVE_", "E2E_LIVE_")
# A relative directory (`projects/`) or file name prefix (`remote-settings.json`); no `..`, no hidden first segment.
VOLATILE_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*/?$")
GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT_S = 30.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class DriverError(Exception):
    """A source or an option is unusable (the message never holds a secret)."""


@dataclass
class Probe:
    """The agent repository's LIVE-03 probe (`scripts/probes/probe_live03.py`) for one profile."""

    python: str
    script: str
    profile: Path
    excluded: tuple[str, ...]
    run: Runner

    def call(self, *args: str, stdout: int | None = None) -> subprocess.CompletedProcess[str]:
        exclusions = [] if args[0] == "compare" else [a for name in self.excluded for a in ("--exclude", name)]
        profile = [] if args[0] == "compare" else ["--profile", str(self.profile)]
        argv = [self.python, self.script, *args, *profile, *exclusions]
        return self.run(argv, text=True, check=False, stdout=stdout, stderr=subprocess.PIPE)

    def snapshot(self, out: Path) -> None:
        if self.call("snapshot", "--out", str(out)).returncode != 0:
            raise DriverError("the profile snapshot failed")

    def token(self) -> str:
        if self.call("check", "--min-valid", str(MIN_VALID_S)).returncode != 0:
            raise DriverError(f"the profile's access token is not valid for {MIN_VALID_S} s (no refresh is done)")
        done = self.call("token", "--min-valid", str(MIN_VALID_S), stdout=subprocess.PIPE)
        value = (done.stdout or "").strip()
        if done.returncode != 0 or not value:
            raise DriverError("the profile's access token could not be read")
        return value

    def compare(
        self, before: Path, after: Path, volatile: tuple[str, ...] = (), *, keychain_only: bool = False
    ) -> dict[str, Any]:
        """The probe's comparison; `ok` unless a difference blocks. Only file differences below a `volatile` prefix
        (session activity of other processes using the same profile: transcripts, shell snapshots) are reported
        without blocking - a keychain attribute change or any other file change always blocks. With `keychain_only`
        (the comparison over the whole run) every file difference is reported as concurrent activity."""
        done = self.call("compare", str(before), str(after), stdout=subprocess.PIPE)
        try:
            report: dict[str, Any] = json.loads(done.stdout or "")
        except ValueError:
            report = {"unchanged": False, "differences": ["the comparison gave no report"]}
        report["unchanged"] = done.returncode == 0 and report.get("unchanged") is True
        differences = [str(item) for item in report.get("differences") or []]
        if not report["unchanged"] and not differences:
            differences = [f"the comparison exited {done.returncode}"]
        report["differences"] = differences

        def concurrent(item: str) -> bool:
            kind, _sep, path = item.partition(": ")
            return kind in FILE_CHANGES and (keychain_only or any(path.startswith(prefix) for prefix in volatile))

        report["volatile"] = [item for item in differences if concurrent(item)]
        report["blocking"] = [item for item in differences if not concurrent(item)]
        report["ok"] = not report["blocking"]
        return report


@dataclass
class Secrets:
    """Secret values by their run variable, and the operator's settings for the run."""

    values: dict[str, str] = field(default_factory=dict)
    settings: dict[str, str] = field(default_factory=dict)

    def stdin(self) -> bytes:
        lines = [f"{name}={value}" for name, value in sorted(self.values.items())]
        lines += [f"{name}={value}" for name, value in sorted(self.settings.items())]
        return ("\n".join(lines) + "\n\n").encode()


def keychain(service: str, run: Runner) -> str:
    """The password of the operator's generic keychain item `service` (read only)."""
    done = run([SECURITY, "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, check=False)
    value = (done.stdout or "").strip()
    if done.returncode != 0 or not value:
        raise DriverError(f"keychain item {service!r} not readable")
    return value


def command_output(command: str, run: Runner) -> str:
    done = run(shlex.split(command), capture_output=True, text=True, check=False)
    value = (done.stdout or "").strip()
    if done.returncode != 0 or not value:
        raise DriverError(f"{shlex.split(command)[0]!r} gave no value")
    return value


def excluded_profiles(options: argparse.Namespace, environ: Mapping[str, str]) -> tuple[str, ...]:
    listed = [name.strip() for name in environ.get(EXCLUDED_ENV, "").split(",")]
    return tuple(dict.fromkeys(name for name in [*options.exclude_profile, *listed] if name))


def gather(options: argparse.Namespace, environ: Mapping[str, str], run: Runner) -> Secrets:
    """The secrets of the configured sources (environment first) and the `AGENT_LIVE_*` settings."""
    secrets = Secrets()
    sources: dict[str, Callable[[], str] | None] = {
        "AGENT_LIVE_OPENROUTER_KEY": (lambda: keychain(options.openrouter_service, run))
        if options.openrouter_service
        else None,
        "AGENT_LIVE_ANTHROPIC_API_KEY": (lambda: keychain(options.anthropic_service, run))
        if options.anthropic_service
        else None,
        "AGENT_LIVE_GITHUB_TOKEN": (lambda: command_output(options.github_token_command, run))
        if options.github_token_command
        else None,
    }
    for name, variable in SECRET_ENV.items():
        source = sources[name]
        value = environ.get(name) or (source() if source is not None else "")
        if value:
            secrets.values[variable] = value
    secrets.settings = {name: environ[name] for name in SETTINGS if environ.get(name)}
    return secrets


def probe_for(options: argparse.Namespace, environ: Mapping[str, str], run: Runner) -> Probe | None:
    if options.profile is None:
        return None
    if not options.probe or not options.probe_python:
        raise DriverError("--profile needs --probe and --probe-python (the agent repository's LIVE-03 probe)")
    excluded = excluded_profiles(options, environ)
    profile = options.profile.expanduser().resolve()
    if profile.name in excluded or options.profile.name in excluded:
        raise DriverError("the operator excluded this profile")
    return Probe(options.probe_python, options.probe, profile, excluded, run)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m live.driver", description=__doc__.splitlines()[0])
    p.add_argument("--result", type=Path, required=True, help="JSON summary (no secret)")
    p.add_argument("--artifacts", type=Path, required=True, help="the run's artefacts after --fetch")
    p.add_argument("--log", type=Path, help="output of the command (scanned as well)")
    p.add_argument("--fetch", help="command that copies the artefacts to --artifacts (runs also after a failure)")
    p.add_argument("--openrouter-service", help="keychain service of the platform OpenRouter key")
    p.add_argument("--anthropic-service", help="keychain service of an Anthropic API key (LIVE-04)")
    p.add_argument("--github-token-command", help="command printing the GitHub token, e.g. 'gh auth token'")
    p.add_argument("--profile", type=Path, help="Claude profile directory chosen by the operator (LIVE-03)")
    p.add_argument("--exclude-profile", action="append", default=[], help="profile name never to use")
    p.add_argument(
        "--volatile",
        action="append",
        default=[],
        help="profile path prefix whose file changes are other processes' session activity (reported, not failing)",
    )
    p.add_argument("--github-cleanup", help="branch prefix of LIVE-02 to clean up after the run (e.g. live02/)")
    p.add_argument("--probe", help="probe_live03.py of the agent repository")
    p.add_argument("--probe-python", help="Python of the agent repository's environment")
    p.add_argument("command", nargs="+", help="transport to the runner device ending in run.sh --live --live-stdin")
    return p


Request = Callable[[urllib.request.Request], tuple[int, Any]]


def urlopen_json(request: urllib.request.Request) -> tuple[int, Any]:
    """Status and JSON answer of one GitHub request (directly from the operator's machine, no proxy)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=GITHUB_TIMEOUT_S) as response:
            data = response.read()
            return response.status, json.loads(data) if data else None
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, None


@dataclass
class GitHubCleaner:
    """The fallback cleanup of LIVE-02 (07-testing.md section 6) after the run - also when the spec was cut off."""

    token: str
    repo: str
    request: Request

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        return self.request(urllib.request.Request(f"{GITHUB_API}{path}", data, headers, method=method))

    def check_private(self) -> None:
        status, answer = self.call("GET", f"/repos/{self.repo}")
        if status != 200 or not isinstance(answer, dict) or answer.get("private") is not True:
            raise DriverError("AGENT_LIVE_GITHUB_REPO is not a private repository the token reaches")

    def cleanup(self, prefix: str) -> dict[str, list[Any]]:
        """Closes open pull requests from `prefix…` branches and deletes those branches; collects every failure."""
        done: dict[str, list[Any]] = {"closed": [], "deleted": [], "errors": []}
        status, pulls = self.call("GET", f"/repos/{self.repo}/pulls?state=open&per_page=100")
        if status != 200 or not isinstance(pulls, list):
            done["errors"].append(f"list pull requests: {status}")
            pulls = []
        for pull in pulls:
            if str(pull.get("head", {}).get("ref", "")).startswith(prefix):
                closed, _ = self.call("PATCH", f"/repos/{self.repo}/pulls/{pull['number']}", {"state": "closed"})
                done["closed" if closed == 200 else "errors"].append(
                    pull["number"] if closed == 200 else f"close: {closed}"
                )
        status, refs = self.call("GET", f"/repos/{self.repo}/git/matching-refs/heads/{prefix}")
        if status != 200 or not isinstance(refs, list):
            done["errors"].append(f"list branches: {status}")
            refs = []
        for ref in refs:
            branch = str(ref.get("ref", "")).removeprefix("refs/heads/")
            deleted, _ = self.call("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}")
            done["deleted" if deleted == 204 else "errors"].append(branch if deleted == 204 else f"delete: {deleted}")
        return done


def github_cleaner(options: argparse.Namespace, secrets: Secrets, request: Request) -> GitHubCleaner | None:
    if not options.github_cleanup:
        return None
    token, repo = secrets.values.get("E2E_LIVE_GITHUB_TOKEN"), secrets.settings.get("AGENT_LIVE_GITHUB_REPO")
    if not token or not repo:
        raise DriverError("--github-cleanup needs the GitHub token and AGENT_LIVE_GITHUB_REPO")
    cleaner = GitHubCleaner(token, repo, request)
    cleaner.check_private()
    return cleaner


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    run: Runner = subprocess.run,
    request: Request = urlopen_json,
) -> int:
    options = parser().parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    # Child processes (keychain, gh, probe, transport, fetch) never see a live value of the operator's environment.
    run = functools.partial(run, env={n: v for n, v in env.items() if not n.startswith(LIVE_PREFIXES)})
    result: dict[str, Any] = {"command_rc": None, "findings": [], "profile": None}
    with tempfile.TemporaryDirectory(prefix="live-driver-") as scratch:
        before, read, after = (Path(scratch) / f"{name}.json" for name in ("before", "read", "after"))
        try:
            if any(not VOLATILE_RE.fullmatch(prefix) for prefix in options.volatile):
                raise DriverError("--volatile takes relative path prefixes such as projects/ or remote-settings.json")
            probe = probe_for(options, env, run)
            if probe is not None:
                probe.snapshot(before)
            secrets = gather(options, env, run)
            if probe is not None:
                secrets.values[SUBSCRIPTION] = probe.token()
                probe.snapshot(read)
            cleaner = github_cleaner(options, secrets, request)
        except DriverError as exc:
            print(f"live.driver: {exc}", file=sys.stderr)
            return EXIT_USAGE
        result["secrets"], result["settings"] = sorted(secrets.values), sorted(secrets.settings)
        log = options.log or Path(scratch) / "command.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log.open("wb") as output:
                done = run(options.command, input=secrets.stdin(), stdout=output, stderr=subprocess.STDOUT, check=False)
            result["command_rc"] = done.returncode
        except OSError as exc:
            result["command_error"] = type(exc).__name__
        if options.fetch:
            result["fetch_rc"] = run(shlex.split(options.fetch), check=False).returncode
        if cleaner is not None:
            result["github_cleanup"] = cleaner.cleanup(options.github_cleanup)
        values = list(secrets.values.values())
        secrets.values.clear()
        options.artifacts.mkdir(parents=True, exist_ok=True)
        result["findings"] = scan(options.artifacts, values) + scan_log(log, values)
        values.clear()
        if probe is not None:
            try:
                probe.snapshot(after)
                at_read = probe.compare(before, read, tuple(options.volatile))
                over_run = probe.compare(before, after, keychain_only=True)
                result["profile"] = {"read": at_read, "run": over_run, "ok": at_read["ok"] and over_run["ok"]}
            except DriverError as exc:
                result["profile"] = {"ok": False, "error": str(exc)}
        options.result.parent.mkdir(parents=True, exist_ok=True)
        options.result.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    unchanged = result["profile"] is None or result["profile"]["ok"]
    cleaned = not result.get("github_cleanup", {}).get("errors")
    ok = result["command_rc"] == 0 and not result["findings"] and unchanged and cleaned
    return EXIT_OK if ok else EXIT_FAILED


def scan_log(path: Path, values: Sequence[str]) -> list[str]:
    """`scan` of the command's log (through a directory that holds only a link to it); findings name `log`."""
    with tempfile.TemporaryDirectory(prefix="live-scan-") as holder:
        (Path(holder) / "log").symlink_to(path)
        return [f"log:{finding.rsplit(':', 1)[1]}" for finding in scan(Path(holder), values)]


if __name__ == "__main__":
    sys.exit(main())
