# HOT-18 – decisions

Package: two faults found while rolling out `0a1207dc`. (1) The build hash of the hub (built in a Linux container)
differed from that of an independent build on macOS. (2) `workvm install` created `lima/_config` of the slot account
as root.

## Part 1 – reproducible web build

1. **Cause: chunk rules on absolute module ids.** HOT-16 stage 2 assigned modules to the chunk groups `start` and
   `screens` with the rule `/[\\/]src[\\/]/` on the module id the bundler hands over. That id is the absolute path. In
   a checkout below a directory named `src`, every library in `node_modules` matched too, so the chunks, their names
   and `index.html` differed. Measured on commit `0a1207dc` with the test root key and the same commit time: macOS
   and the Linux VM in an ordinary directory both gave `5ebdd8c9…`, and macOS below `…/src/agent/web` gave
   `5f75ccca…` (other names: `index-*.js` instead of `start-*.js`). The operating system alone made no difference.
   Before `0a1207dc` the rules (`/src/core/`, `/src/ui/`) did not match library paths, so the hash was stable.
2. **Fix: rules on the path relative to `web/`** (`web/build/chunks.ts`). The groups and their priorities are
   unchanged. In an ordinary checkout the output is byte-identical to before (same `5ebdd8c9…`, same file names), so
   neither the first-load budget nor the time tolerance of HOT-16 moves. A virtual module or one outside `web/` matches
   no group. The React Router global plugin (`build/globals.ts`) also looks at the absolute id, but it does not assign
   chunks. It only rewrites one pinned statement inside React Router, so a parent directory cannot change its output.
   It stays as it is.
3. **Gate (E-19): the Linux part compares builds.** `make platform-ci-linux` (`scripts/guest-ci.sh`, command `run`;
   alone: `web-build`) now also builds `web/` on the runner device (macOS) and in the `guest-ci` VM. Each side builds a
   fresh copy of `web/` with its own `npm ci` (no install scripts), with an empty environment except `PATH`, `HOME`
   and the same `SOURCE_DATE_EPOCH`, `BUILD_COMMIT` and `BUILD_VERSION`. Outside a git checkout these are fixed
   values. The VM copy sits below `/var/lib/guest-ci/src/agent/web`, so the directory differs too, and it uses another
   time zone. `web/scripts/build-compare.mjs` compares the two manifests. On any difference it names the files and
   the step is red. The VM gets Node from the work image pins (`images/versions.json`, checked by SHA-256). The runner
   device uses its own Node. The step logs both versions and warns when they differ, but still compares: equal output
   is required either way. Today both are 26.13.1. `build:check` stays a check on one system, because the
   work-package gates always run the Linux part and GitHub CI has no VM.
4. **Not checked: processor architecture.** Both sides of the gate are arm64. A build on x86-64 is not compared,
   because no test device has one. The bundler's output on the two architectures has not been seen to differ.

## Part 2 – the Lima home of a slot account

5. **`_config` belongs to the slot account (replaces KOR-29 M3, spec 13-work-vm.md section 6.1).** Measured with Lima
   2.3.0 on a test device, as the test account in its own `LIMA_HOME`: with `_config` read-only (0555, user key
   present), `limactl create` works, but `limactl start` fails with `open …/_config/networks.yaml: permission
   denied`. That is the production failure. With `_config` writable, the first start writes only
   `_config/networks.yaml` (0644) and the instance directory. `limactl list` writes nothing. So a root-owned `_config`
   breaks the start. `install` now creates
   `lima/_config` owned by the slot account with mode 0700, like any Lima home. The package orders it, and E-25 puts
   use first. What is given up: a compromised host agent of a slot could now write an `override.yaml` for that one
   slot's later work VMs. Each slot is its own account, and the host packet filter stays the boundary.
6. **Installed devices are repaired by `workvm install`.** For a present slot account, `install` gives
   `lima/_config` to the account with mode 0700, and creates it when it is missing (`accounts.give_lima_config`).
   Root works inside a home that the slot account controls. So `lima` and `_config` are opened with `O_NOFOLLOW` and
   changed only through their descriptors (`fchown`, `fchmod`). A `chown -h` would protect only the last path
   component: a slot could point its `lima/` at another slot's `lima/`, and root would give that `_config` away
   (review finding). A planted link fails the installation with `workvm_install_failed name=lima_config`.
   `after-reboot-check.sh` reports `lima_config_<account>`. It passes only for owner = slot account and mode 0700,
   and the home check fails when `lima/` is a link. The threat model now records the accepted rest: a host-agent
   compromise can leave an `override.yaml` for that slot.
7. **Lima's reason is in the runner log.** `start_failed`, `pool_failed`, `pool_delete_failed` and `delete_failed`
   of the work VMs carry `lima="<last error line>"` after the code. That is the message of Lima's last
   `level=fatal|error` line, else its last line, made printable and cut to 200 characters. It is added only when Lima
   wrote something. Only a line that starts with Lima's `level=` field counts as an error line. The tail can repeat
   the call's arguments. For work VMs these are the instance name, the sizes, the SSH port and the resolvers, none of
   them secret. The session VM pool (`vm/pool.py`) logs Lima's operation without the text and is unchanged.
