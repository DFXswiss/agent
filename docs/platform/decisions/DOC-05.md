# DOC-05 – decisions

Two decisions of the client of 2026-11-25 11:12 entered into the concept file ([`../KONZEPT.md`](../KONZEPT.md))
and their consequences marked in the platform documents.

1. **Cloudflare only on its free plans ([E-10](../KONZEPT.md#e-10)).** "Modelle" in the client's words is read as
   Cloudflare's plans. Added to E-10 as a supplement (same rule: the egress address), not as a new rule, because it
   answers the question about a dedicated egress address that E-10 already withdrew on 2026-11-23. ADR-059 and
   KOR-29 keep their historical wording (addendum 1 asked for a static address); only the follow-up is marked
   "not pursued". Spec 13 §6.3 names the free plans. No other document names a paid Cloudflare plan or add-on as a
   goal (the hub's tunnel and access policy in the cutover plan need none).
2. **No automatic start credit ([E-14](../KONZEPT.md#e-14)).** Credit is only bought (not built: E-14 still has no
   payment path) or distributed by the operator by hand. This answers ADR-079's open question F1; ADR-079, ADR-062,
   spec 12 and KOR-31 mark the start credit as not pursued without rewriting their history. The setting
   `AGENT_HUB_BILLING_START_CREDIT_USD` remains in the code (default 0, no code change); the runbook no longer sets
   it and instead has the operator distribute credit to the existing organizations before the first price list.
3. **Part D, row E-11:** the approvals no longer wait for a start-credit decision; they stay in force until the client
   names the switches to flip.
4. **Dates:** each rule keeps a single date line (checked by `test_konzept.py`); the supplement is written as
   "ergänzt 2026-11-25 11:12" on the same line.
