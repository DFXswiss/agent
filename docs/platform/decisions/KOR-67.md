# KOR-67 – the guide for AI agents holds in a fresh environment

Rules E-26 (operation by AI agents simple and efficient), E-27 (an AI agent can do everything, also without a
person) and E-19 (the class, not the single case).

## Installation (finding 1)

1. **Cause.** Section 0 created the virtual environment with the system `python3`. On a factory-fresh Mac that is
   Python 3.9 with pip 21.2: pip ignores `Requires-Python` of a direct-URL requirement and then backtracks through
   every older release of the dependencies (`watchfiles`, `cryptography` …) that might fit Python 3.9 – many minutes
   at full load, without an end and without a message. `requires-python = ">=3.11"` cannot help against that pip.
2. **The way with the fewest steps that holds:** `uv` from its installer, pinned to one version, into
   `~/.agent-platform-uv` (`UV_UNMANAGED_INSTALL`: no change of shell profiles, an existing `uv` stays), a virtual
   environment with a Python 3.12 that `uv` manages (`--managed-python`: never the system `python3`, so neither the
   old Python of macOS nor the stub of a Mac without the Command Line Tools), the package from the GitHub archive of
   the integration branch (no `git` needed, which a fresh Linux may lack). `--no-config` keeps a user's `uv`
   configuration out. No administrator rights; only `agent-platform` is linked into `~/.local/bin`, so an installed
   `agent` tool stays (unchanged hint). About five seconds on a test device.
3. **Update** is the third line again: `--reinstall-package dfx-agent` fetches the branch archive again and replaces
   the package although its version number does not change; the environment is kept, so a failed download leaves the
   installed tool in place.
4. **Branch of the archive:** `ad5f2026-live`, as before: production follows it; `develop` is the same commit.
5. **No checksum of the installer** beyond the pinned version: the same trust as the former `git+https` install from
   GitHub; the gate runs the pinned installer on every run.
6. **The block runs in the gates as written** (`tests/platform/test_install_block.py`): in an empty environment (fresh
   home, `PATH` of the operating system only) on the gate device – macOS with Apple's `python3` and no Homebrew – and
   in the Linux part (`scripts/guest-ci.sh`, Ubuntu 24.04) as a new user without `sudo`, with `curl` and with the
   `wget` variant the guide names. The one substitution is the package source: an archive of the checkout with the
   layout of GitHub's, so the gate tests the package it ships. The test needs the network of the device (it says so
   when it fails) and is bounded at 60 s.
7. The hint of `release_unavailable` named `pip install --upgrade`; it now names the guide topic `install-tool`.

## Every example runs (goal 2, findings 2 to 8)

8. **Where the examples run:** the real-hub suite of the tool (`tests/platform/tool/realhub/test_examples.py`: the
   tool over HTTP against `agent_hub` on the test PostgreSQL, a platform device of the device double), not a browser
   E2E spec: it is the hub-and-runner-double layer the package names, it runs in every gate run and needs no VM. Each
   command's `example` (the text of `search` and `--help`, which `test_lookup` already keeps literally in the guide)
   runs with the identifiers of a prepared account; optional parts are included, each alternative of a choice runs
   once, `--wait N` is cut to one second. Allowed: success, or a refusal with `next`; never `invalid_argument`,
   `request_invalid` or `internal`. The commands that change the account run last; `recover` uses the code
   `recovery new` printed just before. The exceptions (with reason and the test that covers each instead) are
   `EXCEPTIONS` in the test: the browser and passkey steps (`pair`, `register`, `passkey add`, `agents confirm`),
   what installs or removes something on the computer that runs the test (`device add|remove`, `claude service
   install|remove`, `install-connector`), what needs a person's login (`claude login`, `claude setup-token`) or the
   GitHub CLI (`github connect`), and the servers that run until stopped (`claude watch`, `community serve`, `mcp`).
9. The first run found, besides finding 2, two examples the tool itself refused: `org member-name` without its
   required `--name`, and `community messages` with `--before` and `--after`, which exclude each other. Both
   examples (and the guide) are corrected.
10. **`task list --state open` (finding 2):** the hub filters by one task state of its own (`scheduling` … `closed`);
    `open` is the tool's word. The tool pages through the workgroup's tasks and leaves out the closed ones (at most
    50 tasks, at most 10 pages), so it works with every hub version; no hub change. The hub double of the tool tests
    accepted any state and is now refused like the hub (`request_invalid`) – that is why the class went unnoticed.
11. **`revocable` (finding 3):** one rule (`credentials.revocable`): a token the device flow minted, `ghu_` or
    `gho_`, as the web and `github status` already had; `github accounts` knew only `ghu_` and ignored the source.
12. **Keys over ssh (finding 4):** measured on a test Mac over ssh: the login keychain is locked ("User interaction is
    not allowed"), writing fails at once, so the tool fell back to `agent.key` silently. The trap is a keychain that
    is open while pairing (screen session, or unlocked): later ssh sessions cannot read it and got `state_damaged`
    with "pair again". Now: over ssh (`SSH_CONNECTION`, `SSH_CLIENT`, `SSH_TTY`) the keys go to `agent.key` at once;
    a keychain that cannot be read is `state_damaged`/`keychain_unreadable` with `security unlock-keychain` as next
    step; a hanging `security` call counts as failed instead of ending in `internal`; every result naming the
    connected agent prints `keys` (where they are). `device add` over ssh without `sudo` rights stops before the
    download with `install_failed`/`admin_dialog_over_ssh` (macOS shows no dialog there) instead of failing after it.
13. **`no_device` (finding 5):** since KOR-49/KOR-63 the text named `device assign` but still ended "try again
    later"; it now names `device assign` and `device add --this-computer` and no waiting.
14. **Golden of a new device (finding 6):** cause: `postinstall` starts the maintenance run at once after the
    enrollment, but the runner's store still starts, its status is unreadable, so the run reported `not_enrolled`
    and built nothing; the next run 15 minutes later built it. The run now asks again while the runner is loaded
    (at most 60 × 2 s). A session that finds no ready golden is refused `capacity_exhausted` (`golden`) instead of
    `vm_start_failed`: the hub's existing re-scheduling (ADR-049/ADR-075) keeps the task waiting and starts it by
    itself; it is shown as waiting for capacity (`capacity_exhausted`, whose next step now names a device still
    building its image) – a separate label "device sets itself up" would need a new waiting reason in hub and web
    and is left as a follow-up. `device add` reports `ready` and, until then, a `next`; `--wait` waits up to 30
    minutes.
15. **Lima directories (finding 7):** `~/.lima` and `~/Library/Caches/lima` that did not exist at the installation are
    recorded (`maintenance.json`, `created`) and removed with the device: Lima's `_config` and the cache, then
    `~/.lima` only when it is empty (an instance the owner made with Lima stays and is reported under `leftovers`).
16. **`model_access_unavailable` (finding 8):** the hub already starts the task again by itself once the owner stores
    model access (ADR-079, also for tasks on an own device: the attempt is placed like any other). The tool now gives
    `next` in `task show` and `task follow` for a failed task (`diagnose.FAILED`: `claude connect` or
    `credentials set-api-key`) and for a waiting one (`diagnose.WAITING`).
