# KOR-20 – Claude Code on the runner host (shared documentation)

The change lives in the agent repository (doctor check `login_cli`, see its `decisions/KOR-20.md`). This repository
takes the shared documents unchanged: `spec/05-runner.md`, `runbooks/runner-installation.md`,
`runbooks/golden-image-release.md`, `runbooks/claude-code-upgrade-gate.md`, `CUTOVER.md` (§3.2, S9).

Legacy-untouched test after the merge into `develop`: the test compares with the merge base of `origin/develop`.
Before the merge every platform file was new there (`A`); since `develop` holds the platform, a change of a platform
file shows as `M` and was refused. Platform paths are now accepted with any status; files outside the platform areas
keep the old rules (new test with a platform file on the base).
