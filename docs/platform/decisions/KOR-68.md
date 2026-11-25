# KOR-68 – decisions

## Stage 0: settings without a chosen organization

1. **Where the active org comes from.** A page outside `/o/:org` opened directly starts without an active org. The
   shell (`ui/shell/layout.tsx`) now makes the landing org active once the browser is unlocked and none is active,
   for every such page, not only `/settings`; so the credit badge of the header and every section of the active org
   have one. The landing org is the last one used on this browser, else the first that is **not private**, else the
   first (`AppSession.landingOrg`, also the target of `/`): a team organization is where model access and credit
   usually matter.
2. **Choice in place.** "Model access of the organization" and "Credit and usage" name the active org in a select of
   the user's orgs (`ui/settings/ActiveOrg.tsx`); choosing one activates it and makes it the last used. A section
   keeps its answer together with the org it belongs to and shows nothing of another org after a change. Without any
   org the line says "You are not a member of any organization yet." and nothing shows "Loading …".
3. **Two quota sections told apart, not merged.** "Quotas" (`GET /v1/me/quotas`) is the user's own subscription,
   the same in every organization; it is now titled "My own subscription" with a one-line explanation. The
   organization's section is titled "Model access of the organization". Merging them would hide the own subscription
   behind an organization; the full view of all subscriptions follows in the later stages of KOR-68.
