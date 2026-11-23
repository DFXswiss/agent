# Runbook: golden image release

Scope: building, activating and retiring the golden Lima VM on a runner device
([05 §3.1](../spec/05-runner.md#31-golden-image), [§3.5](../spec/05-runner.md#35-lifecycle-rules), ADR-009, ADR-029).
Inputs live in the agent repository: `images/golden.yaml`, `images/versions.json` (pins), `images/provision/`,
`images/files/`, `src/agent_guest/` (contract of the build: `images/README.md`).

## When

- A change of `images/`, `src/agent_guest/` or the contracts the guest validates (any merge that touches them).
- A Claude Code version bump – only after the upgrade gate passed
  ([claude-code-upgrade-gate.md](claude-code-upgrade-gate.md)).
- A new Ubuntu cloud image or Node LTS pin in `images/versions.json` (URL **and** digest/sha256 change together).

## Build (on each runner device, as the runner account)

1. Update the agent checkout of the runner installation to the release commit; `agent-runner doctor` must be green
   except `golden` (Lima ≥ 2.3, `vz`, store, hub).
2. `agent-runner image build` (optionally `--claude-version <V>`: must equal the pin, else the build refuses). The
   build creates `golden-build-<hex>`, provisions it, computes `exec-paths.json`, records `build.json`
   (`claude_version`, `guest_version`, `landlock_abi`, `exec_paths_sha256`, `built_at`, TUI start time), runs the
   smoke tests (guest services healthy, `claude --version`, sandbox self-test of 05 §4.5, TUI start under Landlock),
   stops the VM and renames it `golden-<claude>-<guest>-<yyyymmdd>`. Target < 2 min. Any failed check, or Landlock
   ABI < 4, removes the build; the previous golden stays `ready`. Lima errors are appended to
   `$AGENT_RUNNER_HOME/log/image.log`.
3. `agent-runner image list` – the new version is `ready`; exactly one golden is `ready` for new clones.
4. `agent-runner doctor` – `golden ok`, `landlock ok` (ABI from the build record).

## Rollout to running workgroups

No action needed: a workgroup VM keeps its golden version for its lifetime. At the next start of a stopped VM on an
older golden without a `suspended` session, the runner replaces it by a VM of the newest golden; stopped sessions
restart fresh and read their history with `get_task_messages`. Pool VMs of the old golden are deleted and refilled.
Watch the device dashboard (pool refilled, VM probe green) and the alert `agent-runner-vm-probe-failed`.

## Retire

Activating a new golden marks the previous one `retired` at once; it stays on disk while VMs still reference it.
`agent-runner image retire <version>` deletes a retired golden that no VM references any more (refused while one does,
and `golden_current` for the ready one).

**Rollback:** there is no command to reactivate a retired golden. Roll back by checking out the previous release
commit of the agent repository and running `agent-runner image build` again (a new golden of the old inputs becomes
`ready`); VMs move to it at their next start as above. Test a release on one runner device before the others.

## Test-mode goldens and shared caches (development only)

E2E and attack-suite runs build TEST_MODE goldens (`image build --test-model-port <port>`, ADR-036) or clone the shared
cache `cache-golden-<sha12>` with `image adopt` (TEST_MODE only, ADR-027 §7). Never build or adopt a TEST_MODE golden
on a production runner; doctor warns `test_mode_on` when `AGENT_RUNNER_TEST_MODE` is not `0`.
