# Decisions of ACC-M0 (acceptance of milestone 0)

1. **Traceability report per requirement.** The M0 criterion asks that `test_traceability.py` reports the M0
   requirements as green. The check only printed one summary line, so it now lists every requirement due up to the
   checked milestone with its present tests. Tests count only if they are due by that milestone: ATK-33..40 (M2)
   share the evidence path `tests/platform/atk` with ATK-01..32 and would otherwise appear as present in M0.
2. **ADR numbers unique across both repositories.** ADR-028 and ADR-029 were written in the agent repository only;
   they are copied into agent-core (and the index) so that a later ADR in agent-core cannot reuse their numbers.
3. **Specification text aligned with the implementation** (reported by VM-01, VM-02, SBX-01): `03-api.md` §7 names the
   Lima parameter as the control-token transport (ADR-009), ADR-027 §7 names all inputs of the golden-cache hash.
   Package decision records that quote the old text stay unchanged as historical records.
4. **ADR-029 measurement values unchanged.** The review noted that ADR-029 quotes the unbudgeted `tui_up` row (0.38 s)
   instead of `tui_start` (p95 0.40 s); both are far below the 2 s budget and the ADR records PRB-01's own reading, so
   the acceptance document gives both values instead of rewriting the ADR.
5. **GitHub Actions.** No workflow run is created for the integration branches; this is outside both repositories.
   By `07-testing.md` §8 the local gates are binding; the draft PRs carry the local CI report.
