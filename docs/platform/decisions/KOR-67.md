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
