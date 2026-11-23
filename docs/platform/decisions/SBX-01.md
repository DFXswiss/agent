# SBX-01 – Golden image definition, managed settings, harness text, nftables, guest units

Decisions taken where the specification (05-runner.md sections 3.1, 4.1–4.3, 5.1, 8, 15) was silent or ambiguous.

1. **Two provisioning stages.** A Lima template cannot carry the `agent_guest` package tree, so `golden.yaml`
   provisions only what needs no repository content (slim steps of Spike A, token hand-over); the stage-2 installer
   `images/provision/install.sh` runs inside the build VM from a copy of `images/` and `src/agent_guest/` (build
   contract in `images/README.md`, driven by VM-01). Both stages are idempotent; the spec's guard
   `/var/lib/agent-golden.done` ends stage 2, `/var/lib/agent-golden/slim.done` ends the slim step that Lima runs on
   every boot.
2. **Claude Code tarballs.** The npm package `@anthropic-ai/claude-code@2.1.330` is a wrapper whose postinstall copies
   the native binary from `@anthropic-ai/claude-code-linux-arm64`. Both tarballs are pinned by sha256 in
   `images/versions.json` (`claude_code_tarball_*`, `claude_code_native_tarball_*`; the sha256 values match the
   registry's sha512 integrity). The installer runs `npm install --global --offline` with both files into a scratch
   prefix with its own npm cache (deleted afterwards, as are `/root/.npm` and Claude's root config), so nothing
   unpinned is fetched, and copies the native ELF binary to `/opt/claude/claude`, the real binary path recorded with
   the source package and the binary's sha256 in `/opt/claude/install.json`.
3. **Node.** Node 26.13.1 (current LTS "Lithium") from the nodejs.org linux-arm64 tarball to `/opt/node`; it needs
   `libatomic1`, which the cloud image lacks.
4. **Module names shared with GST-01.** The units start exactly `python3 -E -s -m agent_guest.control` (root) and
   `python3 -E -s -m agent_guest.server` (`platform`) with `WorkingDirectory=/opt/agent-guest`; the package lives in
   `/opt/agent-guest/agent_guest/`. `-I` is not used because it drops the working directory from `sys.path`; `-E -s`
   ignore `PYTHON*` variables and the user site. GST-01 must not rename these modules.
5. **Unit ordering.** Ubuntu's `cloud-final.service` is ordered after `multi-user.target`; a unit after
   `cloud-final.service` that is wanted by `multi-user.target` would form an ordering cycle. All three units are
   therefore `WantedBy=cloud-init.target`. `agent-guest-control` and `agent-guest-api` require and follow the harden
   unit: if the hardening fails on any boot, the guest services stay stopped and the runner sees an unhealthy VM
   (fail closed). The harden unit is skipped while `/run/agent-golden-build` exists; the installer writes this flag,
   so a build can start the services for its smoke tests and keep its own sudo until the VM stops (a flag on `/run`
   cannot survive into a clone). `systemctl start --job-mode=ignore-requirements` was tried in the test build and
   still activated the harden unit.
6. **Control token hand-over.** Lima passes parameters only to provisioning scripts (`PARAM_*`) and on its root-only
   configuration disk (`/mnt/lima-cidata`, `dmode=700,fmode=700`). The inline system step stages the token on a
   root-only tmpfs (`/run/agent-guest-param`); the harden unit validates it (43 base64url characters), writes
   `/etc/agent-guest/control-token` (root 0600) atomically and deletes the staged copy. Without a parameter it keeps
   the installed token (restart of a stopped workgroup VM). Removing the parameter from the host-side instance
   configuration is the runner's step (VM-02, 05 section 3.2). 03-api.md section 7 still mentions a `limactl shell`
   write; 05 section 3.2 and ADR-009 (parameter before boot) apply.
7. **Sudo of the Lima default user.** The harden unit first deletes cloud-init's `90-cloud-init-users` grant, so
   that any later failure leaves the user without sudo. The user name comes from `LIMA_CIDATA_USER` in
   `/mnt/lima-cidata/lima.env`.
   Instead of the wildcard `systemctl restart agent-guest-*` the sudoers rule lists the four exact commands, because a
   sudoers `*` also matches blanks and would allow restarting further units. The harden unit removes the user from
   `sudo`/`admin`/`wheel`, deletes every other `/etc/sudoers.d` file that grants the user, checks the rendered rule
   with `visudo` and sets `agent-ctl` to exactly that user.
8. **No implicit port forwards.** Lima appends a fallback rule that forwards every guest port bound to 127.0.0.1;
   two `ignore: true` rules (`0.0.0.0` and `127.0.0.1`, any protocol) keep 127.0.0.1:7700 and every other port inside
   the guest (verified in the test build: "Not forwarding TCP 127.0.0.1:7700"). SSH agent and X11 forwarding are off.
9. **nftables loading and TEST_MODE.** `/etc/nftables.conf` is replaced by `flush ruleset` plus
   `include "/etc/nftables.d/*.nft"`, loaded by `nftables.service`. The rule file is exactly 05 section 4.3. TEST_MODE
   (`install.sh --test-model-port PORT`) adds `agent-sessions.test.nft`, which inserts one `platform` rule for the
   model-double port on the Lima host gateway; the gateway address is resolved from `host.lima.internal` at build
   time, so no address appears in this repository.
10. **Runtime directories** come from `/etc/tmpfiles.d/agent-guest.conf`: `/run/agent-guest` and its `harness/`
    0711, `/var/lib/agent-guest` and `/etc/agent-guest` 0700 (all root).
11. **Read deny of the runtime directory.** `Read(//run/agent-guest/**)` is denied as a whole; the session's own
    harness file is given to Claude Code through `--append-system-prompt-file` at start, not through the Read tool
    (05 section 4.2, parenthesis).
12. **Settings file format.** Sorted keys, two-space indent, trailing newline, written atomically with mode 0644; the
    golden fixture `tests/platform/guest/sandbox/fixtures/managed-settings.json` is the byte-exact expectation.
13. **Contract cross-checks.** The MCP tool list and the harness operation examples are compared with
    `contracts/mcp/tools/` and `contracts/ops/` once CTR-02 is synchronised; until then the tests skip with that reason
    and the tables of 03-api.md sections 8.1 and 9 are the reference.
14. **Slim purge.** Only installed packages are purged (one unknown name aborted the whole purge of Spike A's
    command); `needrestart` is purged as well, because it restarts services during every apt run of the build. The
    apt timers are disabled before the purge and every apt call waits up to 300 s for the dpkg lock; the installer
    refuses to run when the slim step did not finish.
15. **Re-running the installer** on a finished image is a no-op only for the same TEST_MODE choice; a different
    `--test-model-port` fails instead of silently keeping the recorded mode.
