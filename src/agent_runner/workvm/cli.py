"""`agent-runner workvm install|status` and `workvm firewall render|check` (docs/platform/spec/13-work-vm.md 6.1-6.4).

| Command | Effect |
|---|---|
| `workvm install --runner-account NAME --quota-gib N [--plan]` | volume, accounts, sudoers, filter; `--plan` dry |
| `workvm uninstall [--keep-volume]` | remove everything `install` created, then report what is left (root) |
| `workvm firewall render [--egress utunN] [--bandwidth-mbit M]` | print the anchor text for the slot accounts |
| `workvm firewall check` | compare the live filter with the rendered anchor; exit 1 on a problem |
| `workvm status` | slots, self mode and the boundary as in `check` (read-only) |
| `workvm image build [--name NAME]` | build and register the work image (`image.Builder`, runner account) |
| `workvm image list` | one fact per row of `runner.workvm_image` |

`install` (root) builds everything from the pure functions of `accounts` and `firewall`: the APFS volume
`accounts.VOLUME_NAME` at `accounts.HOME_ROOT` (skipped when that path is already a mount point), the slot group and
the slot accounts `<prefix>wvm00` ... (skipped when present; new IDs are the smallest free UID/GID from `dscl`), the
`limactl` wrapper (0755), the sudoers rule (0440, checked by `visudo` before it is written) and the host filter
(`firewall.install_plan` / `firewall.apply`: anchor files, the root egress watcher and its LaunchDaemon, the
fail-closed anchor `com.apple/000.agent.workvm` loaded alone - the main ruleset and `/etc/pf.conf` are never touched).
`install` and `install --plan` first run `firewall.preflight`: without Apple's hook in the running main ruleset the
installation is refused (`workvm_firewall_unhooked`). Egress is the device's own Cloudflare client (see `firewall`).
Every written file belongs to root:wheel. One fact per step: `runner.workvm.install step=<step> ...`, then
`runner.workvm.install result=ok slots=<n>`.

`render`, `check` and `status` read the slot UIDs with `/usr/bin/id -u`; `check` and `status` read the filter through
`sudo -n` (the exact read commands of the sudoers rule) unless run as root and print
`runner.workvm.firewall ok=yes|no internet=yes|no egress=<utunN|->` plus `runner.workvm.firewall.problem code=<code>`
per problem. `status` prints `runner.workvm.status slots=<n> self=yes|no accounts=<present> missing=<missing>` first;
in self mode (TEST_MODE, no host filter) and without slots it prints no boundary.

`image build` needs configured slots. It builds from `WORKVM_TEMPLATE` (default `images/workvm.yaml`) in the
runner's own Lima home (`image_lima`, replaced by tests) as `<prefix>wimg-<yyyymmdd>-<6 hex>` (`--name` replaces
`<yyyymmdd>-<6 hex>`), exports the disk to `service.IMAGES_DIR` unless in self mode, and records the image `ready` in
the runner store (started for the command when it does not run, like `agent-runner image build`). One fact per step
`runner.workvm.image step=<step> name=<instance> seconds=<s>`, then `runner.workvm.image name=<name> state=ready
seconds=<s>`; Lima's stderr tail of a failed step goes to `$AGENT_RUNNER_HOME/log/image.log` (0600). `image list`
prints `runner.workvm.image name=<name> state=<state> built_at=<UTC>` per row and `runner.workvm.images count=<n>`.

Commands and file writes go through `Machine` (`default_machine()`, replaced by tests; legacy injection style,
07-testing.md section 1); the effective UID comes from `Host.geteuid`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from agent_runner.config import Config
from agent_runner.errors import RunnerError
from agent_runner.identity import ensure_private_dir
from agent_runner.vm import cli as vm_cli
from agent_runner.workvm import accounts, firewall, image, service

EXIT_OK = 0
EXIT_FAILURE = 1
BANDWIDTH_MBIT = 500
ID = "/usr/bin/id"
LOOKUP_TIMEOUT = 10.0
ROOT_UID = 0
WHEEL_GID = 0


def run_command(argv: Sequence[str], timeout: float, text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run `argv` (no shell), optionally with `text` on standard input; a tool that cannot start counts as failed."""
    try:
        return subprocess.run(list(argv), input=text, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(list(argv), 1, "", "")


def run_input(argv: Sequence[str], text: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return run_command(argv, timeout, text)


def file_writer(uid: int, gid: int) -> firewall.WriteFile:
    """A writer that replaces `path` atomically with `content`, owned by `uid:gid` with `mode`."""

    def write(path: str, content: str, mode: int) -> None:
        target = Path(path)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chown(temporary, uid, gid)
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    return write


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def remove_file(path: str) -> None:
    Path(path).unlink(missing_ok=True)


def remove_empty_dir(path: str) -> None:
    """Remove `path` when it is an empty directory; anything else stays (and `uninstall` reports it)."""
    with contextlib.suppress(OSError):
        Path(path).rmdir()


@dataclass(frozen=True)
class Machine:
    """What the commands touch on the host."""

    run: firewall.Run
    run_input: accounts.RunInput
    write_file: firewall.WriteFile
    read_file: firewall.ReadFile
    is_mount: Callable[[str], bool]
    remove_file: firewall.RemoveFile
    exists: Callable[[str], bool]
    remove_dir: Callable[[str], None]


def default_machine() -> Machine:
    """The real host (tests replace this function)."""
    return Machine(
        run=run_command,
        run_input=run_input,
        write_file=file_writer(ROOT_UID, WHEEL_GID),
        read_file=read_file,
        is_mount=os.path.ismount,
        remove_file=remove_file,
        exists=os.path.lexists,
        remove_dir=remove_empty_dir,
    )


image_lima = vm_cli.make_lima  # the runner's own Lima (tests replace it)


def register(subparsers: Any) -> None:
    workvm = subparsers.add_parser("workvm", help="install and check the work VM slots and host filter")
    actions = workvm.add_subparsers(dest="workvm_command", metavar="ACTION", required=True)
    install = actions.add_parser("install", help="create the volume, slot accounts, sudoers and host filter (root)")
    install.add_argument("--runner-account", required=True, help="unprivileged account that runs the daemon")
    install.add_argument("--quota-gib", type=int, required=True, help="quota of the APFS volume of the slot homes")
    install.add_argument("--bandwidth-mbit", type=int, default=BANDWIDTH_MBIT, help="per slot and direction")
    install.add_argument("--plan", action="store_true", help="print what would change, change nothing")
    install.set_defaults(handler=cmd_install)
    uninstall = actions.add_parser("uninstall", help="remove everything install created and check it is gone (root)")
    uninstall.add_argument("--keep-volume", action="store_true", help="keep the APFS volume of the slot homes")
    uninstall.set_defaults(handler=cmd_uninstall)
    status = actions.add_parser("status", help="slots, self mode and the boundary (read-only)")
    status.set_defaults(handler=cmd_status)
    fw = actions.add_parser("firewall", help="render or check the pf anchor com.apple/000.agent.workvm")
    fw_actions = fw.add_subparsers(dest="workvm_firewall_command", metavar="ACTION", required=True)
    render = fw_actions.add_parser("render", help="print the anchor text for the slot accounts")
    render.add_argument("--egress", metavar="utunN", help="tunnel interface of the internet pass rule (default none)")
    render.add_argument("--bandwidth-mbit", type=int, default=BANDWIDTH_MBIT, help="per slot and direction")
    render.set_defaults(handler=cmd_firewall_render)
    check = fw_actions.add_parser("check", help="compare the live filter with the rendered anchor")
    check.set_defaults(handler=cmd_firewall_check)
    images = actions.add_parser("image", help="build and list the work images")
    image_actions = images.add_subparsers(dest="workvm_image_command", metavar="ACTION", required=True)
    build = image_actions.add_parser("build", help="build the work image from images/workvm.yaml (runner account)")
    build.add_argument("--name", help="name suffix instead of <yyyymmdd>-<6 hex>")
    build.set_defaults(handler=cmd_image_build)
    listing = image_actions.add_parser("list", help="list the registered work images")
    listing.set_defaults(handler=cmd_image_list)


def _root(ctx: Any) -> bool:
    return bool(ctx.host.geteuid() == ROOT_UID)


def _require_slots(config: Config) -> None:
    if config.workvm_slots == 0:
        raise RunnerError("workvm_not_configured", "WORKVM_SLOTS")
    if config.workvm_slots_self:
        raise RunnerError("workvm_slots_self", "WORKVM_SLOTS_SELF")


def _slot_accounts(config: Config) -> list[tuple[int, str, int]]:
    return accounts.slot_accounts(config.vm_prefix, config.workvm_slots, config.workvm_ssh_port_base)


def _uid(run: firewall.Run, account: str) -> int | None:
    result = run([ID, "-u", account], LOOKUP_TIMEOUT)
    text = result.stdout.strip()
    return int(text) if result.returncode == 0 and text.isdigit() else None


def _lookup(config: Config, run: firewall.Run) -> tuple[list[firewall.Slot], list[str]]:
    """The slots whose account exists and the names of the missing accounts."""
    found: list[firewall.Slot] = []
    missing: list[str] = []
    for index, account, port in _slot_accounts(config):
        uid = _uid(run, account)
        if uid is None:
            missing.append(account)
        else:
            found.append(firewall.Slot(index=index, account=account, uid=uid, ssh_port=port))
    return found, missing


def _slots(config: Config, run: firewall.Run) -> list[firewall.Slot]:
    found, missing = _lookup(config, run)
    if missing:
        raise RunnerError("workvm_account_missing", missing[0])
    return found


def _boundary(ctx: Any, run: firewall.Run, slots: Sequence[firewall.Slot], problems: Sequence[str] = ()) -> int:
    """Print the boundary facts; `problems` given = not checked (fail-closed). Returns the exit code."""
    if problems:
        status = firewall.Status(tuple(problems), None)
    else:
        status = firewall.check(run if _root(ctx) else firewall.sudo_run(run), slots, BANDWIDTH_MBIT)
    ctx.out.fact(
        "workvm.firewall",
        {"ok": not status.problems, "internet": status.internet, "egress": status.egress or "-"},
    )
    for code in status.problems:
        ctx.out.fact("workvm.firewall.problem", {"code": code})
    if status.table_missing:
        ctx.out.fact("workvm.firewall.table_missing", {"entries": ",".join(status.table_missing)})
    return EXIT_FAILURE if status.problems else EXIT_OK


def cmd_firewall_render(args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    _require_slots(config)
    text = firewall.render(_slots(config, default_machine().run), args.egress, args.bandwidth_mbit)
    if ctx.out.json_mode:
        ctx.out.fact("workvm.firewall.anchor", {"text": text})
    else:
        ctx.host.stdout.write(text)
        ctx.host.stdout.flush()
    return EXIT_OK


def cmd_firewall_check(_args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    _require_slots(config)
    run = default_machine().run
    return _boundary(ctx, run, _slots(config, run))


def cmd_status(_args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    run = default_machine().run
    found, missing = ([], []) if config.workvm_slots_self else _lookup(config, run)
    ctx.out.fact(
        "workvm.status",
        {
            "slots": config.workvm_slots,
            "self": config.workvm_slots_self,
            "accounts": len(found),
            "missing": len(missing),
        },
    )
    if config.workvm_slots and not config.workvm_slots_self:
        _boundary(ctx, run, found, ("accounts_missing",) if missing else ())
    return EXIT_OK


def _run_all(run: firewall.Run, commands: Sequence[Sequence[str]]) -> None:
    for argv in commands:
        if run(argv, accounts.COMMAND_TIMEOUT).returncode != 0:
            raise RunnerError("workvm_install_failed", argv[0].rsplit("/", 1)[-1])


def _ids(run: firewall.Run, path: str, key: str) -> set[int]:
    """The IDs of `dscl . -list <path> <key>` (lines `name id`)."""
    result = run([accounts.DSCL, ".", "-list", path, key], accounts.COMMAND_TIMEOUT)
    if result.returncode != 0:
        raise RunnerError("workvm_install_failed", "dscl")
    ids: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            ids.add(int(parts[1]))
    return ids


def _group_gid(run: firewall.Run, group: str) -> int | None:
    """The GID of the slot group, None when the group does not exist."""
    result = run([accounts.DSCL, ".", "-read", f"/Groups/{group}", "PrimaryGroupID"], accounts.COMMAND_TIMEOUT)
    if result.returncode != 0:
        return None
    value = result.stdout.split(":", 1)[-1].strip()
    if not value.isdigit():
        raise RunnerError("workvm_install_failed", "dscl")
    return int(value)


def _install_volume(machine: Machine, quota_gib: int) -> str:
    if machine.is_mount(accounts.HOME_ROOT):
        return "present"
    container = accounts.apfs_container(machine.run)
    _run_all(machine.run, accounts.apfs_volume_commands(container, quota_gib))
    return "created"


def _install_group(run: firewall.Run, prefix: str, used: set[int]) -> tuple[str, int, str]:
    """The slot group's name and GID and whether it was created."""
    group = accounts.group_name(prefix)
    gid = _group_gid(run, group)
    if gid is not None:
        return group, gid, "present"
    gid = accounts.free_uid(used)
    _run_all(run, accounts.group_commands(prefix, gid))
    return group, gid, "created"


def _install_accounts(
    run: firewall.Run, planned: Sequence[tuple[int, str, int]], group: str, gid: int, used: set[int]
) -> tuple[list[firewall.Slot], int]:
    """The slots (present accounts kept, missing ones created) and the number created."""
    slots: list[firewall.Slot] = []
    created = 0
    for index, account, port in planned:
        uid = _uid(run, account)
        if uid is None:
            uid = accounts.free_uid(used)
            _run_all(run, accounts.create_commands(account, uid, gid, group, f"{accounts.HOME_ROOT}/{account}"))
            created += 1
        used.add(uid)
        slots.append(firewall.Slot(index=index, account=account, uid=uid, ssh_port=port))
    return slots, created


def _plan_slots(run: firewall.Run, planned: Sequence[tuple[int, str, int]], used: set[int]) -> list[firewall.Slot]:
    """The slots as `install` would set them up (present accounts with their UID, missing ones with the UID they
    would get), without creating anything."""
    slots: list[firewall.Slot] = []
    for index, account, port in planned:
        uid = _uid(run, account)
        if uid is None:
            uid = accounts.free_uid(used)
        used.add(uid)
        slots.append(firewall.Slot(index=index, account=account, uid=uid, ssh_port=port))
    return slots


def _step_fact(step: firewall.InstallStep) -> dict[str, object]:
    if step.action == "run":
        return {"action": "run", "argv": " ".join(step.argv), "may_fail": step.may_fail}
    fact: dict[str, object] = {"action": step.action, "path": step.path}
    if step.action == "write":
        fact |= {"mode": f"{step.mode:04o}", "sha256": hashlib.sha256(step.content.encode()).hexdigest()[:16]}
    return fact


def _print_plan(args: argparse.Namespace, ctx: Any, config: Config, machine: Machine, problems: Sequence[str]) -> int:
    """`install --plan`: what `install` would change, read-only."""
    run, out = machine.run, ctx.out
    planned = _slot_accounts(config)
    volume = "present" if machine.is_mount(accounts.HOME_ROOT) else "create"
    out.fact(
        "workvm.plan", {"step": "volume", "action": volume, "path": accounts.HOME_ROOT, "quota_gib": args.quota_gib}
    )
    out.fact("workvm.plan", {"step": "images", "path": f"{accounts.HOME_ROOT}/images"})
    used = _ids(run, "/Users", "UniqueID") | _ids(run, "/Groups", "PrimaryGroupID")
    group = accounts.group_name(config.vm_prefix)
    gid = _group_gid(run, group)
    out.fact("workvm.plan", {"step": "group", "action": "present" if gid is not None else "create", "name": group})
    used.add(gid if gid is not None else accounts.free_uid(used))
    slots = _plan_slots(run, planned, used)
    missing = sum(1 for _, account, _ in planned if _uid(run, account) is None)
    out.fact("workvm.plan", {"step": "accounts", "count": len(slots), "create": missing})
    out.fact("workvm.plan", {"step": "wrapper", "path": accounts.WRAPPER_PATH})
    out.fact("workvm.plan", {"step": "sudoers", "path": accounts.SUDOERS_PATH})
    for step in firewall.install_plan(slots, args.bandwidth_mbit):
        out.fact("workvm.plan.firewall", _step_fact(step))
    out.fact(
        "workvm.plan",
        {
            "main_ruleset": "untouched",
            "pf_conf": "untouched",
            "anchor": firewall.ANCHOR,
            "hook": "ok" if not problems else "missing",
        },
    )
    for code in problems:
        out.fact("workvm.plan.problem", {"code": code})
    return EXIT_FAILURE if problems else EXIT_OK


def cmd_install(args: argparse.Namespace, ctx: Any) -> int:
    if not _root(ctx):
        raise RunnerError("workvm_root_required")
    config: Config = ctx.config(require=frozenset())
    _require_slots(config)
    machine = default_machine()
    run, out = machine.run, ctx.out
    planned = _slot_accounts(config)
    names = [account for _, account, _ in planned]
    sudoers_text = accounts.sudoers(args.runner_account, names)
    firewall.render([], None, args.bandwidth_mbit)  # validates the bandwidth before anything changes
    if _uid(run, args.runner_account) is None:
        raise RunnerError("workvm_account_missing", "runner_account")
    problems = firewall.preflight(run)
    if args.plan:
        return _print_plan(args, ctx, config, machine, problems)
    if problems:
        raise RunnerError("workvm_firewall_unhooked", problems[0])

    volume = _install_volume(machine, args.quota_gib)
    out.fact("workvm.install", {"step": "volume", "action": volume, "path": accounts.HOME_ROOT})
    images = f"{accounts.HOME_ROOT}/images"  # service.IMAGES_DIR: the runner writes clone templates, slots read
    owner = ["/usr/sbin/chown", args.runner_account, images]
    _run_all(run, [["/bin/mkdir", "-p", images], owner, ["/bin/chmod", "0755", images]])
    out.fact("workvm.install", {"step": "images", "path": images})

    used = _ids(run, "/Users", "UniqueID") | _ids(run, "/Groups", "PrimaryGroupID")
    group, gid, action = _install_group(run, config.vm_prefix, used)
    used.add(gid)
    out.fact("workvm.install", {"step": "group", "action": action, "name": group, "gid": gid})

    slots, created = _install_accounts(run, planned, group, gid, used)
    out.fact("workvm.install", {"step": "accounts", "count": len(slots), "created": created})

    machine.write_file(accounts.WRAPPER_PATH, accounts.wrapper_script(names), 0o755)
    out.fact("workvm.install", {"step": "wrapper", "path": accounts.WRAPPER_PATH})

    if not accounts.sudoers_check(machine.run_input, sudoers_text):
        raise RunnerError("workvm_sudoers_invalid")
    machine.write_file(accounts.SUDOERS_PATH, sudoers_text, 0o440)
    out.fact("workvm.install", {"step": "sudoers", "path": accounts.SUDOERS_PATH})

    plan = firewall.install_plan(slots, args.bandwidth_mbit)
    firewall.apply(plan, run, machine.write_file, machine.read_file, machine.remove_file)
    out.fact("workvm.install", {"step": "firewall", "steps": len(plan), "anchor": firewall.ANCHOR})
    out.fact("workvm.install", {"result": "ok", "slots": len(slots)})
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace, ctx: Any) -> int:
    """Remove everything `install` created and check that it is gone (the main ruleset is never loaded)."""
    if not _root(ctx):
        raise RunnerError("workvm_root_required")
    config: Config = ctx.config(require=frozenset())
    _require_slots(config)
    machine = default_machine()
    run, out = machine.run, ctx.out
    names = [account for _, account, _ in _slot_accounts(config)]
    _stop_slots(run, names)  # while the filter still holds: no VM runs unfiltered after the flush
    plan = firewall.uninstall_plan()
    firewall.apply(plan, run, machine.write_file, machine.read_file, machine.remove_file)
    out.fact("workvm.uninstall", {"step": "firewall", "steps": len(plan), "anchor": firewall.ANCHOR})
    _stop_slots(run, names)  # whatever started in between
    removed = 0
    for account in names:
        if _uid(run, account) is not None:
            _run_all(run, [accounts.delete_command(account)])
            removed += 1
    out.fact("workvm.uninstall", {"step": "accounts", "count": len(names), "removed": removed})
    group = accounts.group_name(config.vm_prefix)
    group_action = "absent"
    if _group_gid(run, group) is not None:
        _run_all(run, [accounts.delete_group_command(group)])
        group_action = "removed"
    out.fact("workvm.uninstall", {"step": "group", "action": group_action, "name": group})
    for path in (accounts.WRAPPER_PATH, accounts.SUDOERS_PATH):
        machine.remove_file(path)
    out.fact("workvm.uninstall", {"step": "files", "wrapper": accounts.WRAPPER_PATH, "sudoers": accounts.SUDOERS_PATH})
    volume = "kept" if args.keep_volume else "absent"
    if not args.keep_volume and machine.is_mount(accounts.HOME_ROOT):
        _run_all(run, [accounts.delete_volume_command()])
        volume = "removed"
    if not args.keep_volume:
        machine.remove_dir(accounts.HOME_ROOT)  # the mount point `install` created (only when empty)
    out.fact("workvm.uninstall", {"step": "volume", "action": volume, "path": accounts.HOME_ROOT})
    left = _left_over(machine, names, group, volume=not args.keep_volume)
    for code in left:
        out.fact("workvm.uninstall.problem", {"code": code})
    out.fact("workvm.uninstall", {"result": "ok" if not left else "incomplete"})
    return EXIT_FAILURE if left else EXIT_OK


def _stop_slots(run: firewall.Run, names: Sequence[str]) -> None:
    """Kill every process of the slot accounts (their VMs); none running is fine."""
    for account in names:
        run([firewall.PKILL, "-KILL", "-u", account], accounts.COMMAND_TIMEOUT)


def _left_over(machine: Machine, names: Sequence[str], group: str, *, volume: bool) -> list[str]:
    """What an uninstall left behind (empty = the state before `install`)."""
    run = machine.run
    left: list[str] = []
    anchor = run((firewall.PFCTL, "-a", firewall.ANCHOR, "-sr"), firewall.PFCTL_TIMEOUT)
    if anchor.stdout.strip():
        left.append("anchor_rules")
    files = (
        firewall.DAEMON_PATH,
        firewall.WATCH_PATH,
        firewall.ANCHOR_PATH,
        firewall.ANCHOR_UP_PATH,
        firewall.ANCHOR_SAMPLE_PATH,
        firewall.STATE_PATH,
        accounts.WRAPPER_PATH,
        accounts.SUDOERS_PATH,
    )
    left += [f"file:{path}" for path in files if machine.exists(path)]
    left += [f"account:{name}" for name in names if _uid(run, name) is not None]
    if _group_gid(run, group) is not None:
        left.append(f"group:{group}")
    if firewall.without_legacy_lines(machine.read_file(firewall.PF_CONF)) != machine.read_file(firewall.PF_CONF):
        left.append("pf_conf")
    if volume and machine.is_mount(accounts.HOME_ROOT):
        left.append("volume")
    elif volume and machine.exists(accounts.HOME_ROOT):
        left.append(f"dir:{accounts.HOME_ROOT}")
    return left


def _image_log(config: Config) -> Callable[[str], None]:
    def log(text: str) -> None:
        ensure_private_dir(config.log_dir)
        descriptor = os.open(
            config.log_dir / vm_cli.IMAGE_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(text)

    return log


def cmd_image_build(args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    if config.workvm_slots == 0:
        raise RunnerError("workvm_not_configured", "WORKVM_SLOTS")
    ensure_private_dir(config.home)
    ensure_private_dir(config.tmp_dir)

    def announce(name: str, fields: dict[str, str | int | bool]) -> None:
        ctx.out.fact(name, fields)

    with (
        vm_cli.store(config, ctx) as dsn,
        tempfile.TemporaryDirectory(dir=config.tmp_dir, prefix="workvm-image-") as scratch,
    ):
        builder = image.Builder(
            lima=image_lima(config, ctx.host.env),
            template=config.workvm_template or service.default_template(),
            scratch=Path(scratch),
            register=lambda name, at: image.register_ready(dsn, name, at),
            export_dir=None if config.workvm_slots_self else service.IMAGES_DIR,
            announce=announce,
            log=_image_log(config),
        )
        built = builder.build(args.name)
    ctx.out.fact("workvm.image", {"name": built.name, "state": "ready", "seconds": f"{built.seconds:.3f}"})
    return EXIT_OK


def cmd_image_list(_args: argparse.Namespace, ctx: Any) -> int:
    config: Config = ctx.config(require=frozenset())
    with vm_cli.existing_store(config, ctx) as dsn:
        rows = [] if dsn is None else image.list_images(dsn)
    for row in rows:
        ctx.out.fact(
            "workvm.image",
            {
                "name": row.name,
                "state": row.state,
                "built_at": row.built_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    ctx.out.fact("workvm.images", {"count": len(rows)})
    return EXIT_OK
