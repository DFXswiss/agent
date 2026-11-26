# ADR-009: VM start below ten seconds (KONZEPT §14.3)

Status: Accepted (2026-11-21)

## Context
KONZEPT §3: on the first task of a new organization/workgroup, VM and session must be ready in < 10 s; the UI shows a countdown. Spike A on the large runner device (Lima 2.3.0, vz, Ubuntu 24.04, 4 vCPU/8 GiB/20 GiB):
- warm-pool assignment 0.10 s (median, max 0.13 s);
- clone of a slimmed golden VM + start 4.3–6.0 s, also with 35 and 100 running VMs; `limactl clone` 0.04 s (APFS clonefile);
- cold start from cloud image 8.1–11.5 s (too slow);
- snapshots/suspend not available with vz; SIGSTOP pause keeps RAM;
- ~1 GB host RAM per idle VM; RAM is not returned to the host (high-water mark) until the VM stops.

## Decision
- **Golden image** per release (slim Ubuntu, pinned Node, pinned Claude Code, guest agent, sandbox assets), versioned `golden-<claude>-<guest>-<yyyymmdd>`, built outside the hot path.
- **Warm pool** of neutral running clones (default 2, configurable), no workgroup data; assignment = DB mapping + `POST /v1/assign` + session sandbox. Refill asynchronously.
- **Fallback** order (identical in `03-api.md` §4.6.2 and `05-runner.md` §6.1): running VM of the workgroup → stopped VM of the workgroup restart (4.1–5.9 s; if a newer golden exists and no session is suspended, the stopped VM is deleted instead and the next step applies) → pool → clone + start. Cold start never in the hot path.
- Hot-path savings: the sandbox self-test runs only at golden build, pool fill and guest version change; the control token is passed as a Lima param before boot instead of a post-boot `limactl shell` write.
- **Idle stop** after 30 min without sessions; delete on unassignment/archive.
- **Capacity**: VM memory budget = host RAM − reserve (default 32 GiB); slots = budget / VM memory (large runner device: 56 × 8 GiB).
- Acceptance metric: `first_working_at − task.created_at` < 10 s (hub clock), measured for pool and clone paths (PERF).

## Consequences
- Countdown estimates come from the scheduler (`03-api.md` §4.6.2).
- Claude Code start-up and prompt acceptance inside the sandbox are not yet measured end to end (Spike A measured `claude --version` only; guest services, socket forwarding, TUI start under Landlock and the hub round trips are not included). M0 measures the TUI start under Landlock and the clone path end to end; M1 must prove the budget. The clone path margin is thin (≈ 6 s measured + unmeasured steps) – recorded as a risk.
- Workgroup VMs keep their golden version for their lifetime and are replaced by a fresh clone when idle-stopped after a newer golden exists and no session is suspended.

## Addendum (2026-11-26): pool fixed at 1
Decision of the client of 2026-11-26: the warm pool is **fixed at 1** - on every device, platform devices and own
devices, existing and new ones. It is no longer a setting: the size is a constant in the code
(`agent_runner.config.POOL_SIZE`), `AGENT_RUNNER_POOL_SIZE` is not read any more. This replaces "default 2,
configurable" above, the 4 of the platform device and the "no warm pool" (`POOL_SIZE=0`) of own devices (ADR-074 §4);
the installation of an own device sets no pool setting.
- A device whose `runner.env` or plist still sets `AGENT_RUNNER_POOL_SIZE` starts anyway (no `config_invalid`); the
  daemon reports it once at start (`runner.config_ignored name=POOL_SIZE pool_size=1`) and keeps one pool VM.
- Refill as before (after every assignment and every 60 s), back up to one ready VM; the second task of a burst takes
  the clone path, which stays below ten seconds (Spike A: 4.3–6.0 s).
- An own device keeps one more running VM than before (its memory counts against the capacity budget above).
- The clone path is still measured (PERF-02, E2E-07): the E2E suite holds the pool empty with `TEST_POOL_EMPTY=1`,
  which the runner accepts only in test mode (`TEST_MODE=1`, never in production; otherwise `config_invalid`).

## Alternatives considered
Cold start (too slow), snapshots (unsupported), pausing VMs (no RAM gain, unsupported), one shared VM per device (violates per-workgroup VM).

## Deviation from DESIGN.md
None.
