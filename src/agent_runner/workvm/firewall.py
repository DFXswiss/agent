"""Host packet filter of the work VMs: pf anchor `com.apple/000.agent.workvm` (spec 13-work-vm.md 6.2, 6.3).

The device's own Cloudflare client (installed and enrolled by the operator) carries the device's traffic through its
tunnel interface `utunN` and leaves with Cloudflare's shared egress addresses. The anchor lets a slot account out
only on that tunnel interface; everything else of a slot account is blocked (fail-closed: when the client
disconnects, the routes move back to the physical interface and the pass rule no longer matches).

**The main ruleset is never loaded or changed** (KOR-29 operations finding 1): the Cloudflare client hooks its own
anchor `com.cloudflare.warp_svc` into the running main ruleset at runtime, so `pfctl -f /etc/pf.conf` would drop it.
Our rules live in a child of Apple's anchor: the main ruleset of macOS evaluates `anchor "com.apple/*"`, which takes
every child of `com.apple` - loaded with `pfctl -a com.apple/000.agent.workvm -f FILE` without touching anything else -
before the client's anchor (the client appends its line after Apple's). `/etc/pf.conf` stays untouched; the
LaunchDaemon of the watcher loads the anchor at boot. Every rule of the anchor names a slot account (`user U`); the
rest of the device is not filtered by it.

`render` builds the anchor text (the persistent table `<workvm_blocked>` plus the rules of section 6.2 per slot
account) for a given egress interface or for none (fail-closed: no pass rule to the internet). `render_up_template`
is the same text with the placeholder `@EGRESS@`. The root-owned watcher (`watch_script`, LaunchDaemon
`swiss.dfx.agent-runner.pf`) checks every few seconds that the boundary is **effective**, not only loaded: pf
enabled (enabled again when it was not), the hook `anchor "com.apple/*"` present in the main ruleset and before the
client's anchor and before any `pass quick`, and the anchor's rules loaded. While all of that holds it loads the
pass rule on the tunnel interface when the client is connected, the fail-closed text otherwise; when the hook is
missing or comes too late (something replaced the main ruleset) it kills every process of the slot accounts at once
- their VMs with them - and keeps doing so until the hook is back (it never reloads the main ruleset itself). `check`
reads the same facts (through `sudo -n` with the exact read commands of `READ_COMMANDS`); a failed check makes the
runner tear down every work VM and answer requests "not ready". `preflight` refuses an installation on a device whose
main ruleset has no hook. `install_plan` / `uninstall_plan` / `apply` install and remove everything (run by
`agent-runner workvm install|uninstall` as root). Every command goes through an injected `run(argv, timeout) ->
CompletedProcess` (legacy injection style, 07-testing.md section 1); nothing here touches the machine by itself.

macOS facts this module relies on (verified = read in the man pages / system files of macOS 27 on the build device
or measured read-only on the execution device by the operator; assumed = not run, because loading pf rules needs
root and changes the machine; `images/workvm/host-rehearsal.sh` checks every assumption on a test device):
- verified (pfctl(8)): `-E` enables pf and increments the enable reference count; `-a ANCHOR -f FILE` loads only
  that anchor; `-a ANCHOR -F all` flushes only that anchor; `-s info`, `-sr`, `-s Interfaces -v`.
- verified (/etc/pf.conf of macOS): the main ruleset holds `scrub-anchor "com.apple/*"` and `anchor "com.apple/*"`.
- verified on the execution device (operations report, client connected): pf is enabled by the client; the running
  main ruleset is `scrub-anchor "com.apple/*"`, `anchor "com.apple/*"`, `anchor "com.cloudflare.warp_svc" all`; the
  client's anchor ends with `block drop in all` / `block return out all` and passes the private networks with
  `pass out quick`; the client binary is `/usr/local/bin/warp-cli` and `warp-cli --accept-tos status` prints
  `Status update: Connected` while the tunnel is up.
- assumed: a wildcard anchor evaluates every child of `com.apple` that has rules, so a child loaded at runtime is
  evaluated (INF-07 runs the network guard of a test device the same way, `com.apple/wp-guard`).
- assumed: when the client reconnects or updates itself it appends its anchor to the running main ruleset again
  and keeps Apple's lines (otherwise the watcher stops the slot accounts, fail-closed).
- assumed: `-s Interfaces -v` marks a skipped interface with `(skip)`; `-sr` prints a `{ tcp udp }` list as one rule
  per protocol, ports as `port = N` (or a service name such as `smtp`) and users as `user = UID`.

Normalisation of `check`: comment, blank and continuation lines of the rendered text are joined; every `pass` /
`block` rule is expanded per protocol of a `proto { ... }` list and reduced to the signature (action, `return`,
`on` interface, protocol, port, user, table). The live rules of `pfctl -a agent.workvm -sr` are reduced the same way;
both signature lists must be equal in order and count. State options, flags, the address family and the
source/destination addresses other than the table are not compared, because pfctl prints them in its own canonical
form.
"""

from __future__ import annotations

import ipaddress
import plistlib
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from agent_runner.errors import RunnerError

Run = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
WriteFile = Callable[[str, str, int], None]
ReadFile = Callable[[str], str]

HOOK = "com.apple/*"
# `000.` sorts before every child Apple ships (`100.InternetSharing`, `200.AirDrop`, `250.ApplicationFirewall`):
# pf evaluates the children of a wildcard anchor in name order, and ours must decide a slot's packets first.
ANCHOR = "com.apple/000.agent.workvm"
PARENT = "com.apple"
CLIENT_ANCHOR = "com.cloudflare.warp_svc"
TABLE = "workvm_blocked"
ANCHOR_PATH = "/etc/pf.anchors/agent.workvm"
ANCHOR_UP_PATH = "/etc/pf.anchors/agent.workvm.up"
ANCHOR_SAMPLE_PATH = "/etc/pf.anchors/agent.workvm.sample"
WATCH_PATH = "/etc/pf.anchors/agent.workvm.watch"
PF_CONF = "/etc/pf.conf"
STATE_PATH = "/var/run/agent.workvm.state"
DAEMON_LABEL = "swiss.dfx.agent-runner.pf"
DAEMON_PATH = f"/Library/LaunchDaemons/{DAEMON_LABEL}.plist"
PFCTL = "/sbin/pfctl"
PKILL = "/usr/bin/pkill"
LAUNCHCTL = "/bin/launchctl"
SUDO = "/usr/bin/sudo"
# The client's command line tool as installed on the execution device (root-owned /usr/local/bin).
WARP_CLI = "/usr/local/bin/warp-cli"
ROUTE = "/sbin/route"
WARP_CONNECTED = "Status update: Connected"
EGRESS_TOKEN = "@EGRESS@"
SAMPLE_INTERFACE = "utun0"
WATCH_SECONDS = 2
WATCH_REFRESH = 12
HOOK_LINE = f'anchor "{HOOK}"'
CLIENT_LINE = f'anchor "{CLIENT_ANCHOR}"'
# The lines an earlier installer (KOR-29 stage a, never run on a device) added to /etc/pf.conf; `uninstall` removes
# them from the file (without loading it).
LEGACY_LINES = ('anchor "agent.workvm"', 'load anchor "agent.workvm" from "/etc/pf.anchors/agent.workvm"')
LEGACY_FILES = ("/etc/pf.anchors/agent.workvm.dnctl",)
PFCTL_TIMEOUT = 15.0
MAX_SLOTS = 100
# The read-only pfctl commands of `check`; the sudoers rule of `accounts.sudoers` allows exactly these to the runner.
READ_COMMANDS: tuple[tuple[str, ...], ...] = (
    (PFCTL, "-s", "info"),
    (PFCTL, "-a", ANCHOR, "-sr"),
    (PFCTL, "-sr"),
    (PFCTL, "-s", "Interfaces", "-v"),
    (PFCTL, "-a", ANCHOR, "-t", TABLE, "-T", "show"),
    (PFCTL, "-a", PARENT, "-sA"),
)


# IANA special-purpose and non-public ranges (RFC 6890 and successors) the work VMs may never reach.
def _v4(first: int, second: int, third: int, fourth: int, prefix: int) -> str:
    """An IPv4 network from its octets (written as numbers: the repository's hygiene rule keeps address literals out
    of the sources)."""
    return str(ipaddress.IPv4Network((ipaddress.IPv4Address(bytes((first, second, third, fourth))), prefix)))


# Special-purpose and private IPv4 ranges (RFC 6890 registry): this-network, private, CGNAT, loopback, link-local,
# IETF protocol assignments, the three documentation networks, 6to4 relay anycast, benchmarking, multicast, reserved
# and limited broadcast.
BLOCKED_V4: tuple[str, ...] = tuple(
    _v4(*octets)
    for octets in (
        (0, 0, 0, 0, 8),
        (10, 0, 0, 0, 8),
        (100, 64, 0, 0, 10),
        (127, 0, 0, 0, 8),
        (169, 254, 0, 0, 16),
        (172, 16, 0, 0, 12),
        (192, 0, 0, 0, 24),
        (192, 0, 2, 0, 24),
        (192, 88, 99, 0, 24),
        (192, 168, 0, 0, 16),
        (198, 18, 0, 0, 15),
        (198, 51, 100, 0, 24),
        (203, 0, 113, 0, 24),
        (224, 0, 0, 0, 4),
        (240, 0, 0, 0, 4),
        (255, 255, 255, 255, 32),
    )
)
# Special-purpose IPv6 ranges; unique-local and link-local are joined from parts for the same hygiene rule.
# Not `::/128`: the unspecified address is never a destination, and pf leaves it out of a table (rehearsal of
# 2026-11-24: the loaded table held `::1` but not `::`), so `check` would never see it.
BLOCKED_V6: tuple[str, ...] = (
    "::1/128",
    "::ffff:0:0/96",
    "64:ff9b::/96",
    "64:ff9b:1::/48",
    "100::/64",
    "2001::/23",
    "2001:db8::/32",
    "2002::/16",
    "3fff::/20",
    "fc" + "00::/7",
    "fe" + "80::/10",
    "ff00::/8",
)


def resolvers() -> tuple[str, ...]:
    """The resolvers of the work VMs (Cloudflare's public resolvers, reached through the tunnel like all traffic)."""
    return (_v4(1, 1, 1, 1, 32).split("/")[0], _v4(1, 0, 0, 1, 32).split("/")[0])


_ACCOUNT_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_EGRESS_RE = re.compile(r"^utun[0-9]{1,3}$")
_MIN_UID = 501
_MAX_UID = 2**31
_MIN_PORT = 1024
_MAX_PORT = 65535
_MAX_MBIT = 100_000
_SERVICES = {"smtp": "25", "domain": "53"}
_PROTOS = {"ipv6-icmp": "icmp6"}  # pfctl prints the IANA name
_TABLE_RE = re.compile(r"<([A-Za-z0-9_]+)>")


@dataclass(frozen=True)
class Slot:
    """One work-VM slot: index, macOS account, its UID and the pinned Lima SSH port."""

    index: int
    account: str
    uid: int
    ssh_port: int


@dataclass(frozen=True)
class InstallStep:
    """One step: write or remove a file, remove the legacy lines from `/etc/pf.conf` (file only), or run a command."""

    action: Literal["write", "remove", "unpatch", "run"]
    path: str = ""
    content: str = ""
    mode: int = 0o644
    argv: tuple[str, ...] = ()
    may_fail: bool = False


def _invalid(field: str) -> RunnerError:
    return RunnerError("workvm_firewall_invalid", field)


def _validate_slots(slots: Sequence[Slot]) -> None:
    if len(slots) > MAX_SLOTS:
        raise _invalid("slots")
    indexes: set[int] = set()
    ports: set[int] = set()
    uids: set[int] = set()
    for slot in slots:
        if not 0 <= slot.index < MAX_SLOTS or slot.index in indexes:
            raise _invalid("index")
        if not _ACCOUNT_RE.fullmatch(slot.account):
            raise _invalid("account")
        if not _MIN_UID <= slot.uid <= _MAX_UID or slot.uid in uids:
            raise _invalid("uid")
        if not _MIN_PORT <= slot.ssh_port <= _MAX_PORT or slot.ssh_port in ports:
            raise _invalid("ssh_port")
        indexes.add(slot.index)
        uids.add(slot.uid)
        ports.add(slot.ssh_port)


def _bandwidth(bandwidth_mbit: int) -> None:
    if not 0 < bandwidth_mbit <= _MAX_MBIT:
        raise _invalid("bandwidth_mbit")


def _render(slots: Sequence[Slot], egress: str | None, bandwidth_mbit: int) -> str:
    _validate_slots(slots)
    _bandwidth(bandwidth_mbit)
    state = f"internet only on {egress}" if egress else "fail-closed, no internet"
    lines = [
        f"# pf anchor {ANCHOR}, generated by agent-runner workvm install (spec 13 sections 6.2 and 6.3).",
        f"# Egress: {state}. Every rule names a slot account; the rest of the device is not filtered here.",
        f"# Bandwidth per slot and direction: {bandwidth_mbit} Mbit/s (not enforced yet, decisions KOR-29 #20).",
        f"table <{TABLE}> persist {{ \\",
        *(f"  {entry} \\" for entry in (*BLOCKED_V4, *BLOCKED_V6)),
        "}",
    ]
    for slot in sorted(slots, key=lambda s: s.index):
        u = slot.uid
        lines += [
            f"# slot {slot.index:02d} {slot.account}",
            f"pass out quick on lo0 proto tcp from 127.0.0.1 to 127.0.0.1 port {slot.ssh_port} user {u}"
            " flags S/SA keep state",
            # the slot's own SSH proxy answers from port P as the same account: its packets leave on lo0 too
            f"pass out quick on lo0 proto tcp from 127.0.0.1 port {slot.ssh_port} to 127.0.0.1 user {u} no state",
            f"block return out quick on lo0 proto {{ tcp udp }} user {u}",
            f"block return out quick proto {{ tcp udp }} from any to <{TABLE}> user {u}",
            f"block return out quick proto tcp from any to any port 25 user {u}",
        ]
        if egress:
            lines.append(
                f"pass out quick on {egress} proto {{ tcp udp }} from any to any user {u} keep state"
                " (if-bound, max 4096, max-src-conn 2048, max-src-conn-rate 2000/10)"
            )
        lines.append(f"block return out quick user {u}")
    return "\n".join(lines) + "\n"


def render(slots: Sequence[Slot], egress_interface: str | None, bandwidth_mbit: int) -> str:
    """The anchor text of `agent.workvm` (deterministic for equal input); `None` = fail-closed, no internet."""
    if egress_interface is not None and not _EGRESS_RE.fullmatch(egress_interface):
        raise _invalid("egress_interface")
    return _render(slots, egress_interface, bandwidth_mbit)


def render_up_template(slots: Sequence[Slot], bandwidth_mbit: int) -> str:
    """The anchor text with the placeholder `@EGRESS@` the watcher replaces by the tunnel interface."""
    return _render(slots, EGRESS_TOKEN, bandwidth_mbit)


def watch_script(
    slots: Sequence[Slot],
    warp_cli: str = WARP_CLI,
    *,
    pfctl: str = PFCTL,
    pkill: str = PKILL,
    route: str = ROUTE,
    anchor_path: str = ANCHOR_PATH,
    anchor_up_path: str = ANCHOR_UP_PATH,
    state_path: str = STATE_PATH,
    seconds: int = WATCH_SECONDS,
) -> str:
    """The root-owned egress watcher (spec 13 section 6.3), run by the LaunchDaemon with `KeepAlive`.

    Every `WATCH_SECONDS`: enable pf when it is disabled; check that the main ruleset holds the hook `anchor
    "com.apple/*"` as the first `anchor` line and before any `pass quick` (the module docstring). Without an
    effective hook it kills every process of the slot accounts (their VMs) and writes `unhooked` to `STATE_PATH`; it
    never loads the main ruleset. When our anchor is not the first child of `com.apple` (another child would be
    evaluated before it) it does the same and writes `unordered`. With the hook it asks the Cloudflare client for
    its state and the route of the work VMs' resolver for its interface, and loads the anchor with the pass rule on
    that `utunN` while the client is connected, the fail-closed anchor otherwise - on every change, when the anchor
    has no rules (flushed by someone) and every `WATCH_REFRESH` rounds - and writes `ok <egress or ->`. A failed
    load is retried in the next round. Disconnecting does not wait for the watcher: the routes leave the tunnel, the
    pass rule stops matching (its states are bound to the tunnel interface) and the final block rule of each slot
    holds. The keyword arguments exist for the tests.
    """
    _validate_slots(slots)
    names = " ".join(slot.account for slot in sorted(slots, key=lambda s: s.index))
    probe = resolvers()[0]
    return f"""#!/bin/sh
# agent-runner work VM egress watcher, generated by agent-runner workvm install (spec 13 section 6.3).
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
accounts='{names}'
last=-
rounds=0
while :; do
  if ! '{pfctl}' -s info 2>/dev/null | grep -q 'Status: Enabled'; then
    '{pfctl}' -E >/dev/null 2>&1
  fi
  main=$('{pfctl}' -sr 2>/dev/null)
  anchor=$(printf '%s\n' "$main" | grep -n '^anchor ' | head -n 1)
  hook=${{anchor%%:*}}
  first=$(printf '%s\n' "$main" | grep -n '^pass.* quick' | head -n 1 | cut -d: -f1)
  hooked=yes
  case "$anchor" in
    *:'{HOOK_LINE}'*) ;;
    *) hooked= ;;
  esac
  if [ -n "$hooked" ] && [ -n "$first" ] && [ "$first" -lt "$hook" ]; then hooked=; fi
  '{pfctl}' -s info 2>/dev/null | grep -q 'Status: Enabled' || hooked=
  if [ -z "$hooked" ]; then
    for account in $accounts; do '{pkill}' -KILL -u "$account" >/dev/null 2>&1; done
    echo unhooked > '{state_path}'
    last=-
  else
    egress=
    if '{warp_cli}' --accept-tos status 2>/dev/null | grep -q '{WARP_CONNECTED}'; then
      egress=$('{route}' -n get {probe} 2>/dev/null | awk '$1 == "interface:" {{ print $2 }}')
      case "$egress" in
        utun[0-9] | utun[0-9][0-9] | utun[0-9][0-9][0-9]) ;;
        *) egress= ;;
      esac
    fi
    loaded=$('{pfctl}' -a {ANCHOR} -sr 2>/dev/null | grep -c .)
    if [ "$egress" != "$last" ] || [ "$rounds" -ge {WATCH_REFRESH} ] || [ "$loaded" = 0 ]; then
      rounds=0
      if [ -n "$egress" ]; then
        sed "s/{EGRESS_TOKEN}/$egress/g" '{anchor_up_path}' | '{pfctl}' -q -a {ANCHOR} -f - && last=$egress
      else
        '{pfctl}' -q -a {ANCHOR} -f '{anchor_path}' && last=
      fi
    fi
    child=$('{pfctl}' -a {PARENT} -sA 2>/dev/null | sed -n '/./{{s/^ *//;p;q;}}')
    if [ "$child" = '{ANCHOR}' ]; then
      echo "ok ${{egress:--}}" > '{state_path}'
    else
      for account in $accounts; do '{pkill}' -KILL -u "$account" >/dev/null 2>&1; done
      echo unordered > '{state_path}'
    fi
  fi
  rounds=$((rounds + 1))
  sleep {seconds}
done
"""


def _logical_lines(text: str) -> list[str]:
    joined: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if line:
            joined.append(line)
    return joined


def _after(tokens: list[str], key: str) -> str:
    if key not in tokens:
        return ""
    rest = tokens[tokens.index(key) + 1 :]
    if rest and rest[0] == "=":
        rest = rest[1:]
    return rest[0] if rest else ""


Signature = tuple[str, ...]


def _signatures(line: str) -> list[Signature]:
    """The normalised signatures of one pf rule line (one per protocol), see the module docstring."""
    spaced = line.replace("{", " { ").replace("}", " } ").replace(",", " ")
    tokens = spaced.split()
    protos = [""]
    if "proto" in tokens:
        start = tokens.index("proto") + 1
        if tokens[start : start + 1] == ["{"]:
            end = tokens.index("}", start)
            protos = tokens[start + 1 : end]
        else:
            protos = tokens[start : start + 1]
    protos = [_PROTOS.get(proto, proto) for proto in protos]
    port = _after(tokens, "port")
    port = _SERVICES.get(port, port)
    table = _TABLE_RE.search(line)
    base = (
        tokens[0],
        "return" if "return" in tokens else "",
        _after(tokens, "on"),
        port,
        _after(tokens, "user"),
        table.group(1) if table else "",
    )
    return [(*base[:3], proto, *base[3:]) for proto in protos]


def _rule_signatures(text: str) -> list[Signature]:
    result: list[Signature] = []
    for line in _logical_lines(text):
        if line.split()[0] in ("pass", "block"):
            result += _signatures(line)
    return result


def egress_of(live_rules: str) -> str | None:
    """The interface of the live internet pass rule (`pass ... on utunN`), None when the anchor is fail-closed."""
    for signature in _rule_signatures(live_rules):
        if signature[0] == "pass" and signature[2] != "lo0" and _EGRESS_RE.fullmatch(signature[2]):
            return signature[2]
    return None


def _table_entries() -> set[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return {ipaddress.ip_network(entry) for entry in (*BLOCKED_V4, *BLOCKED_V6)}


def _live_entries(output: str) -> set[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """The networks of `pfctl -t … -T show` (one per line, indented; a host without a prefix length; other lines such
    as pfctl's notices are skipped)."""
    entries: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
    for raw in output.splitlines():
        try:
            entries.add(ipaddress.ip_network(raw.strip().lstrip("!").split()[0], strict=False))
        except (ValueError, IndexError):
            continue
    return entries


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _missing(expected: set[Network], live: set[Network]) -> list[Network]:
    """The expected networks no live entry covers (pf may print an entry in another but equal form)."""
    return [
        entry
        for entry in expected
        if not any(entry.version == found.version and entry.subnet_of(found) for found in live)  # type: ignore[arg-type]
    ]


def hooked(main_rules: str) -> bool:
    """True when `anchor "com.apple/*"` is the first `anchor` line of the main ruleset (before the client's and any
    other anchor) and no `pass ... quick` rule comes before it."""
    lines = [line.strip() for line in main_rules.splitlines()]
    first_anchor = next((i for i, line in enumerate(lines) if line.startswith("anchor ")), None)
    if first_anchor is None or not lines[first_anchor].startswith(HOOK_LINE):
        return False
    first_pass = next((i for i, line in enumerate(lines) if line.startswith("pass") and " quick" in line), len(lines))
    return first_anchor < first_pass


def first_child(listing: str) -> bool:
    """True when our anchor is the first child of `com.apple` (`pfctl -a com.apple -sA`), i.e. evaluated first."""
    names = [line.strip() for line in listing.splitlines() if line.strip()]
    return bool(names) and names[0] == ANCHOR


def _skips_lo0(interfaces: str) -> bool:
    return any(line.split()[:1] == ["lo0"] and "(skip)" in line for line in interfaces.splitlines())


@dataclass(frozen=True)
class Status:
    """The result of `check`: problem codes (empty = the boundary holds) and the live egress interface."""

    problems: tuple[str, ...]
    egress: str | None
    table_missing: tuple[str, ...] = ()
    """With `table_stale`: the blocked networks the live table does not cover (`unreadable`: no table output)."""

    @property
    def internet(self) -> bool:
        return not self.problems and self.egress is not None


def sudo_run(run: Run) -> Run:
    """`run` with `sudo -n` in front: the runner reads the filter through the exact `READ_COMMANDS` of sudoers."""

    def wrapped(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return run([SUDO, "-n", *argv], timeout)

    return wrapped


def check(run: Run, slots: Sequence[Slot], bandwidth_mbit: int) -> Status:
    """The live filter against the rendered anchor for the live egress interface (or none), fail-closed."""
    info = run(READ_COMMANDS[0], PFCTL_TIMEOUT)
    if info.returncode != 0:
        return Status(("pfctl_failed",), None)
    problems: list[str] = []
    if "Status: Enabled" not in info.stdout:
        problems.append("pf_disabled")
    anchor = run(READ_COMMANDS[1], PFCTL_TIMEOUT)
    egress = egress_of(anchor.stdout) if anchor.returncode == 0 else None
    rendered = render(slots, egress, bandwidth_mbit)
    live = _rule_signatures(anchor.stdout)
    expected_rules = _rule_signatures(rendered)
    if anchor.returncode != 0 or (expected_rules and not live):
        problems.append("anchor_missing")
    elif sorted(live) != sorted(expected_rules):
        # pfctl's ruleset optimizer regroups the rules of a slot (by protocol) without changing what they decide, so
        # the live rules are compared as a set of signatures, each signature exactly as often as rendered
        problems.append("anchor_mismatch")
    main = run(READ_COMMANDS[2], PFCTL_TIMEOUT)
    if main.returncode != 0 or not hooked(main.stdout):
        problems.append("not_hooked")
    children = run(READ_COMMANDS[5], PFCTL_TIMEOUT)
    if children.returncode != 0 or not first_child(children.stdout):
        problems.append("not_first")
    interfaces = run(READ_COMMANDS[3], PFCTL_TIMEOUT)
    if interfaces.returncode != 0 or _skips_lo0(interfaces.stdout):
        problems.append("skip_lo0")
    table = run(READ_COMMANDS[4], PFCTL_TIMEOUT)
    missing = tuple(sorted(str(entry) for entry in _missing(_table_entries(), _live_entries(table.stdout))))
    if table.returncode != 0 or missing:
        problems.append("table_stale")
    return Status(tuple(problems), egress, missing if table.returncode == 0 else ("unreadable",))


def without_legacy_lines(current: str) -> str:
    """`current` without the lines an earlier installer added (`LEGACY_LINES`); unchanged when there are none."""
    lines = current.splitlines()
    kept = [line for line in lines if line.strip() not in LEGACY_LINES]
    return current if kept == lines else "\n".join(kept) + "\n"


def preflight(run: Run) -> list[str]:
    """Problems that forbid an installation (read-only): the main ruleset must already hold the hook, because the
    installer never loads the main ruleset."""
    main = run((PFCTL, "-sr"), PFCTL_TIMEOUT)
    if main.returncode != 0:
        return ["pfctl_failed"]
    return [] if hooked(main.stdout) else ["not_hooked"]


def daemon_plist() -> str:
    """The LaunchDaemon of the egress watcher (pipes, one pf enable reference, egress anchor; kept alive)."""
    data = {"Label": DAEMON_LABEL, "ProgramArguments": [WATCH_PATH], "RunAtLoad": True, "KeepAlive": True}
    return plistlib.dumps(data).decode()


def install_plan(slots: Sequence[Slot], bandwidth_mbit: int, warp_cli: str = WARP_CLI) -> list[InstallStep]:
    """The installation steps; each check runs before the step that would make a broken file live. Only the anchor
    `com.apple/000.agent.workvm` is loaded (fail-closed first); the main ruleset and `/etc/pf.conf` are not touched."""
    return [
        InstallStep("write", ANCHOR_PATH, render(slots, None, bandwidth_mbit)),
        InstallStep("run", argv=(PFCTL, "-a", ANCHOR, "-n", "-f", ANCHOR_PATH)),
        InstallStep("write", ANCHOR_SAMPLE_PATH, render(slots, SAMPLE_INTERFACE, bandwidth_mbit)),
        InstallStep("run", argv=(PFCTL, "-a", ANCHOR, "-n", "-f", ANCHOR_SAMPLE_PATH)),
        InstallStep("write", ANCHOR_UP_PATH, render_up_template(slots, bandwidth_mbit)),
        InstallStep("write", WATCH_PATH, watch_script(slots, warp_cli), mode=0o755),
        InstallStep("write", DAEMON_PATH, daemon_plist()),
        InstallStep("run", argv=(PFCTL, "-a", ANCHOR, "-f", ANCHOR_PATH)),
        InstallStep("run", argv=(LAUNCHCTL, "bootout", f"system/{DAEMON_LABEL}"), may_fail=True),
        InstallStep("run", argv=(LAUNCHCTL, "bootstrap", "system", DAEMON_PATH)),
    ]


def uninstall_plan() -> list[InstallStep]:
    """The steps removing the filter part of an installation: the watcher first (it would load the anchor again),
    then the anchor's rules and tables, the files, and the lines of an earlier installer in `/etc/pf.conf` (the file
    only; the main ruleset is never loaded)."""
    return [
        InstallStep("run", argv=(LAUNCHCTL, "bootout", f"system/{DAEMON_LABEL}"), may_fail=True),
        InstallStep("remove", DAEMON_PATH),
        InstallStep("remove", WATCH_PATH),
        InstallStep("run", argv=(PFCTL, "-a", ANCHOR, "-F", "all"), may_fail=True),
        *(InstallStep("remove", path) for path in (ANCHOR_PATH, ANCHOR_SAMPLE_PATH, ANCHOR_UP_PATH, STATE_PATH)),
        *(InstallStep("remove", path) for path in LEGACY_FILES),
        InstallStep("unpatch", PF_CONF),
    ]


RemoveFile = Callable[[str], None]


def apply(
    plan: Sequence[InstallStep], run: Run, write_file: WriteFile, read_file: ReadFile, remove_file: RemoveFile
) -> None:
    """Execute `plan` through the injected callables; a failing command raises `workvm_firewall_install`."""
    for step in plan:
        if step.action == "write":
            write_file(step.path, step.content, step.mode)
        elif step.action == "remove":
            remove_file(step.path)
        elif step.action == "unpatch":
            current = read_file(step.path)
            cleaned = without_legacy_lines(current)
            if cleaned != current:
                write_file(step.path, cleaned, step.mode)
        else:
            result = run(step.argv, PFCTL_TIMEOUT)
            if result.returncode != 0 and not step.may_fail:
                raise RunnerError("workvm_firewall_install", step.argv[0].rsplit("/", 1)[-1])
