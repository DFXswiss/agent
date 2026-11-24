# DOC-03 – decisions

Concept file brought up to date: interpretations A8–A18 in part C, open deviations of part D checked against the
integration branch ([`../KONZEPT.md`](../KONZEPT.md), part D rule 3).

1. **Numbering A8–A18 in the order of the restplan's questions.** A8 test-run record, A9 one throw-away VM per test
   run, A10 subagents take follow-up instructions, A11 reading across workgroups (intersection of task owner and
   instructing human), A12 support as its own principal, A13 default workgroup write as a default only, A14 no fixed
   limits left, A15 every instruction carries its verified human, A16 no lock on workflow files, A17 no platform
   default for protected branches, A18 blocked attempts audited, kernel refusals counted. Questions that need the
   client (money, contracts, other people's accounts) and proposals without a package are not entered; they are not
   interpretations of the orchestrator.
2. **Part D also lists the new interpretations that the built state does not meet yet** (A8–A13, A15, A17, A18 with
   their closing package). Part D rule 3 asks for every known, not yet built deviation; A14 shares the row of A5, A16
   needs no change (no lock on workflow files exists).
3. **Rules set to "gebaut" where the package is merged:** E-08–E-10 (ADR-059, work VM; switched on in operation
   separately), E-14–E-17 (ADR-062), E-18 (ADR-060), E-22 (ADR-061); E-07 notes that operator subscriptions are now
   charged (ADR-062). The rule sentences and wordings of part B are unchanged; the cross-reference of E-07 to the
   removed part D row now points to idea 46.
4. **No block of its own for interpretations in `spec/01-requirements.md`.** A1–A7 never had rows; the R-E block
   says so, and the affected R-16 rows (K-10.3, K-27, K-28, K-29, K-36, K-43, K-45–K-47) cite the new anchors and the
   current package. K-45 and K-47 are "gebaut" with ADR-062.
5. **The test knows the interpretation anchors.** `interpretation_problems` in `tests/platform/test_konzept.py`
   checks that part C numbers its interpretations gaplessly from A1, each right under its anchor `a-N`, and that the
   table of contents names the range.
