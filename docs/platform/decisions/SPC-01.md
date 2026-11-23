# SPC-01 – E2E-26 in its original order again

1. **Order.** E2E-26 starts the working Research task right after Rita, Carol and Wendy joined (as up to `91fae48`),
   not before the joins (SCN-06 item 16). The first prompt of the new workgroup thus follows the membership changes
   closely - the path that was broken in the runner (fixed by SCN-08 and HOT-03 in `agent`). A workaround in the
   scenario would hide exactly that path.
2. **Kept from later packages.** The wait for the held model request with the evidence on a timeout (the device's
   `prompt_delivery` rows and the session's terminal, now labelled `E2E-26` instead of `SCN-06`) and the strict
   workgroup-name checks of toast and row (KOR-05). No assertion weakened, no timeout raised, no retry added.
3. **Hold back at 90 s.** The model double holds the request after `write` for 90 s again (scenario as in `91fae48`).
   The 150 s of SCN-06 covered three joins inside the hold; in the original order only the entry with notify and the
   ping fall into it, which HOT-03's ten green runs showed to fit. This shortens a wait, it raises no time limit.
4. **Evidence.** E2E-25 and E2E-26 together ten times on T1 and three times on T2 (`tools/wp/e2e-par.sh`, lanes);
   commands and results in the workspace report of this package.
