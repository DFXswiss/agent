# HOT-07 – Integration branch fully green

1. **Web coverage needed no new test.** On the head of the integration branch the merged web coverage (Vitest +
   Playwright) is 100 % lines and branches again. The 24 open branches measured before came from the failing
   `e2e/connect.spec.ts`; the fix of that test (commit `e3467e7e`) closed them. No dead code was found, so no product
   code changed.
2. **Shared documentation copied from `agent`.** `docs/platform/README.md`, `spec/00-overview.md`,
   `spec/01-requirements.md` and `spec/07-testing.md` differed from the copy in `agent` (concept file and its links,
   legacy coverage threshold 86.9 from `develop`). `test_shared_documentation_equals_the_other_repository` fails
   whenever the other checkout is next to this one. The `agent` copy is the newer one, so it was copied here unchanged.
   `KONZEPT.md` came along because the copied files link to it.
3. **Left for later (repository merge):** `KONZEPT.md` is not on the shared list of `test_docs.py`, and
   `test_konzept.py` exists only in `agent`. In this repository nothing yet checks that the two copies of the concept
   file stay the same.
