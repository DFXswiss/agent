# KOR-22 – Registration without the recovery-code step; passkeys must deliver PRF (shared documentation)

The change lives in the agent-core repository (web client and hub, see its `decisions/KOR-22.md`). This repository
takes the shared documents unchanged: ADR-055 (with the note in ADR-006 and the index), `spec/03-api.md` (§4.1
`DELETE /v1/me/registration`), `spec/04-crypto.md` (§2, §3, §4.1, §5.1, §5.5, §5.6, §9), `spec/06-web.md` (§3.1,
§3.1.1, §3.2, §3.3, §3.5, §3.6), `spec/01-requirements.md` (R-1.6), `spec/07-testing.md` (E2E-01, E2E-30),
`spec/00-overview.md`, `PLATFORM-DESIGN.md` and `spec-deviations.md`, and the contracts (`contracts/changes/KOR-22.md`:
one additive endpoint). The plans `spec/09-milestones.md` and `spec/10-work-packages.md` keep their historical wording.
The runner is not affected: it never reads the recovery slot and does not call the new endpoint.
