# Runbook: Claude Code upgrade gate

Scope: changing the pinned Claude Code version of the golden image
([05 §15](../spec/05-runner.md#15-claude-code-updates), [07 §2.3](../spec/07-testing.md), ADR-008, ADR-021, ADR-025).
The same gate applies to every change of the golden image, the managed settings, the harness, the sandbox code or the
model gateway ([07 §8](../spec/07-testing.md#8-ci-definitions), last rule). Claude Code updates are never taken
automatically: the auto-updater is off in the golden (`DISABLE_AUTOUPDATER`, managed settings).

## 1. Prepare the pin (agent repository, own branch)

1. In `images/versions.json` set `claude_code`, both tarball URLs (npm wrapper and native `linux-arm64` package)
   and their `sha256`, computed from the downloaded tarballs (never copied from a third party).
2. Read the release notes of every version between the old and the new pin for: hook events and their payloads,
   managed-settings keys, permission rules, environment variables, TUI start and input box, MCP behaviour, new
   network endpoints. Note each finding in the change's decision file.
3. Run `make platform-ci` (unit tests of `agent_guest.sandbox.settings`, policy and hook handling).

## 2. Gate (all on the runner device, all must pass)

| Step | Command | Pass |
|---|---|---|
| Golden build with the new version | `agent-runner image build --claude-version <V>` in a test home ([golden-image-release.md](golden-image-release.md)) | build green: smoke tests, sandbox self-test, Landlock ABI ≥ 4, TUI start recorded |
| Linux guest suite on that golden | `make platform-ci-linux` (agent) | green, coverage data combined |
| Attack suite ATK-01 … ATK-40, both modes (hooks; hook fault + poisoned settings) and OS probes | `scripts/run-atk.sh --prefix <prefix>` (agent; builds a TEST_MODE golden from the branch) | every attack `pass` in both modes, detector controls fail exactly their injected criteria, no model request without a registered double; results file as in `docs/platform/atk/` |
| Model-double E2E | `e2e/platform/run.sh --topology t1` (agent-core; full suite, desktop) | green; the model double fails on any unknown route or header, so a new upstream call of Claude Code shows here |
| PERF-01 time to working | `e2e/platform/run.sh --topology t1 --only PERF-01` with `E2E_PERF_RUNS=20 E2E_PERF_SHORT_RUNS=10` | every run and p95 < 10 s (07 §5.3); compare with the previous version's numbers |
| Live mini turn (optional, owner decision) | LIVE-03 with a subscription access token, read-only (07 §2.4) | answer received |

Record the results (versions, run IDs, pass counts, PERF numbers) in the change's acceptance file under
`docs/platform/acceptance/` and the ATK results under `docs/platform/atk/`.

## 3. Release

Merge the pin change, then release the golden on every runner device
([golden-image-release.md](golden-image-release.md)). Running workgroup VMs move to the new golden at their next start
(05 §3.5). Rollback = rebuild from the previous pin ([golden-image-release.md](golden-image-release.md#retire)); release
on one runner device first and on the others after a day without incidents.

## 4. When the gate fails

- A hook or settings change in Claude Code: adapt `agent_guest` (policy, hook handling, managed settings) in its
  own change with tests, then run the whole gate again. Never relax the OS layer to make an attack pass.
- A new upstream route: add it to the model double only if the route is legitimate (documented by Anthropic), with
  a test; otherwise the version is refused.
- PERF-01 regression: measure the TUI start in the build record; a slower start above the budget blocks the version.
- Stay on the old pin until all of this is green; there is no partial rollout.
