# Golden image definition

Declarative definition of the golden Lima VM of the platform runner (docs/platform/spec/05-runner.md sections 3.1,
4.1–4.3, 5.1, 8 and 15; ADR-008, ADR-009). The build itself is `agent-runner image build` (VM-01); this directory is
its input.

| Path | Content |
|---|---|
| `golden.yaml` | Lima template: Lima ≥ 2.3.0, `vz`, aarch64, Ubuntu 24.04 arm64 pinned by URL and digest, no mounts, no containerd, only the control-socket forward, parameter `AGENT_GUEST_CONTROL_TOKEN`, Lima provisioning (slim step, token hand-over) |
| `versions.json` | Pins: Claude Code version and the sha256 of its npm wrapper and native linux-arm64 tarballs, Node LTS version and tarball sha256, Ubuntu image URL and digest, URL and sha256 of every wheel of the main orchestrator's agent tool (`tool_wheel_<name>_url`, `tool_wheel_<name>_sha256`) |
| `provision/10-slim.sh` | Lima system provisioning: slim steps of Spike A, guarded by `/var/lib/agent-golden/slim.done` |
| `provision/install.sh` | Stage-2 installer (below), guarded by `/var/lib/agent-golden.done` |
| `files/etc/…`, `files/usr/…` | Installed verbatim (root, 0644, scripts 0755): nftables, systemd units, tmpfiles, the harden script and its sudoers template |
| `files/fragments/` | Fragments the installer merges: fstab line of `/srv/sessions`, TEST_MODE nftables rule |
| `mcp.json`, `harness/RULES.md` | Installed to `/opt/agent-guest/` |
| `mcp-orchestrator.json` | The second MCP server of the main orchestrator's session (`/mcp-orchestrator`), installed to `/opt/agent-guest/`; the guest adds it to that session only after the claim (05-runner.md §17.5) |
| `orchestrator/agent-platform` | Launcher of the agent tool in the guest (`/opt/agent-orch/bin/agent-platform`, isolated interpreter) |

## Build contract

```sh
limactl create -y --name=<prefix>golden-build-<hex> [--cpus N --memory GiB --disk GiB] images/golden.yaml
limactl start -y <prefix>golden-build-<hex>                    # runs provision/10-slim.sh
COPYFILE_DISABLE=1 tar -cf - images src/agent_guest src/agent_platform src/agent_runner src/agent_cli/__init__.py \
  src/agent_cli/keys.py | limactl shell --workdir / <vm> sudo bash -c \
  'mkdir -p /tmp/agent-golden && tar -xf - -C /tmp/agent-golden --no-same-owner'
limactl shell --workdir / <vm> sudo bash /tmp/agent-golden/images/provision/install.sh [--test-model-port PORT]
```

The installer installs `tmux nftables acl` (+ `libatomic1` for Node), Node from the pinned tarball to `/opt/node`,
Claude Code from both pinned npm tarballs (offline `npm install --global` into a scratch prefix) to
`/opt/claude/claude` (`/opt/claude/install.json` records path, source package and sha256), copies `src/agent_guest`
to `/opt/agent-guest/agent_guest` (root, directories 0755, files 0644), generates
`/etc/claude-code/managed-settings.json` with `agent_guest.sandbox.settings`, creates the `platform` user and the
`agent-ctl` group (Lima default user only), mounts `/srv/sessions` (`noexec,nosuid,nodev`, fstab), loads the
nftables rules (`nft -c` first) and enables `agent-guest-harden`, `agent-guest-control` and `agent-guest-api`.
`--test-model-port` builds a TEST_MODE golden: the `platform` user may also reach that port on the Lima host gateway
(resolved from `host.lima.internal` at build time); `/var/lib/agent-golden/install.json` records the mode.

The installer does not start the guest services. Every clone starts them at boot after
`agent-guest-harden.service`, which limits the Lima default user's sudo to restarting the guest services and moves
the control token from the Lima parameter to `/etc/agent-guest/control-token` (root 0600); both services require
the harden unit, so a failed hardening leaves them stopped. During the build the installer writes the flag
`/run/agent-golden-build`, which skips the harden unit until the next boot: a build that needs the services for its
smoke tests writes a temporary token to `/etc/agent-guest/control-token`, starts them with `systemctl start`, keeps
its own sudo, and stops them and deletes that token before `limactl stop`. The installer refuses to run before the slim
step finished and deletes AppleDouble files (`._*`) a macOS `tar` may add to the bundle.

`agent-runner image build` (VM-01) follows this contract with a bundle that also carries the JSON contracts the
guest validates against (copied to `/opt/agent-guest/contracts` after the installer), writes `exec-paths.json` and
`build.json`, runs the smoke tests, and removes the empty `sock/` directory Lima leaves in the stopped instance
(`limactl clone` cannot copy it); docs/platform/decisions/VM-01.md.

Clones: `limactl start -y --set '.param.AGENT_GUEST_CONTROL_TOKEN="<token>"' <clone>`; the runner then removes the
parameter from the instance configuration on the host (05-runner.md section 3.2).

## The agent tool of the main orchestrator

The user's main orchestrator may run inside a platform session (ADR-082 §6 and §9.7; 05-runner.md §17). Its agent tool
is part of the golden image, used only by the system account `agent-orch` (key-model.md KM-37 states the exception):

- `install.sh` creates the group and user `agent-orch` (no login, no home), unpacks the pinned wheels of
  `versions.json` (`TOOL_WHEELS`: `httpx`, `cryptography` and their dependencies, each verified against its sha256
  before use; the image has no `pip`) and copies `src/agent_platform`, `src/agent_runner` and
  `src/agent_cli/{__init__,keys}.py` of the bundle to `/opt/agent-orch/lib`, compiles them and makes the tree
  `root:agent-orch` with directories `0750` and files `0640`; then it imports the tool and draws a test key as
  `agent-orch`, so a build with a broken tool fails.
- `tmpfiles.d/agent-guest.conf` creates `/var/lib/agent-orch` (`0700`, the tool's state with the key) and
  `/run/agent-orch` (`agent-orch:platform 0710`, the tool's MCP socket for the API process only);
  `agent-sessions.nft` drops everything `agent-orch` sends (its hub requests go through the runner's relay).

**Who rebuilds the golden image.** A change of the tool (`src/agent_platform`), of the runner modules it imports
(`src/agent_runner/crypto`, `src/agent_runner/sync`) or of a pinned wheel reaches a VM only with a new golden image:
the operator of the platform devices runs `agent-runner image build` after the release that changed them (the bundle
digest in `build.json` changes; the shared cache tag of the work-package tools does not include the tool's sources, so
a work package that changes the tool must build its golden itself). Order of a release that changes the protocol
between them (ADR-082 Consequences): hub and web first, then the runners, then the golden image. To move a wheel to a
new version, take the `aarch64` (or `py3-none-any`) wheel of `files.pythonhosted.org` for CPython 3.12, write its URL
and sha256 into `versions.json` and keep `TOOL_WHEELS` in `install.sh` the complete set of the tool's third-party
imports (checked by `tests/platform/images/test_provisioning.py`).
