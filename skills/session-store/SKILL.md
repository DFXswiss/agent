The session-store and skill-contract files ship inside the package.

Run `agent skills path` and read `session-store/SKILL.md`, `spine/SKILL.md`,
`review-loop/SKILL.md`, and `pr-review/SKILL.md` from that directory.
`error-fix/SKILL.md` ships in the packaged tree; read it from the package
when an `AGENT_SKILLS_DIR` override omits it. Tests exist to find and
document product defects; DESIGN.md and CONTRIBUTING.md are the contract.
Unset `AGENT_SKILLS_DIR` and run `agent skills path` again to print the
packaged directory.
