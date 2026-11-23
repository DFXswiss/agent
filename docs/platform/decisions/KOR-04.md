# KOR-04 – decisions

Corrections from AUD-01 (A-07, A-08, A-16) in the web client and the hub's `GET /v1/me`.

1. **A-07 – source of org exits: the holders of the current `CK`.** The package text offers "the orgs the user left
   since the last CK state" or "the reader device list of the CK". The hub already stores every `cred` wrap and does
   not delete it when a device or the user leaves an org (the hub deletes only the `org`/`wg` wraps of a removed
   principal, `orgs/append.py` `delete_wraps_of`; 02-data-model.md §2.2 says "deleted when the recipient is removed"
   without naming the scope – DOC-01 must pin that `cred` wraps stay, a hub test asserts it), so
   the most direct and complete answer is the list of devices holding a wrap of the user's current `CK` epoch:
   `GET /v1/me` gets `cred_holders {epoch, devices}` (contract change `contracts/changes/KOR-04.md`, additive). Only
   IDs the hub stores as wrap recipients, no content (zero knowledge unchanged). A list of left orgs would need new
   state at the hub (when was the "last CK state"?) and would still miss holders that lost eligibility in other ways.
2. **A-07 – how the browser uses it.** A holder that no verified log of an org the user belongs to (per `GET /v1/me`)
   entitles is a loss "now" (key `<device> holds <epoch>`), merged with the losses from the logs; the rotation itself
   is unchanged (new wraps only for the readers of the verified logs, so the other org's device gets none). The hub can
   only cause a rotation with this list, never suppress one the logs owe. To avoid rotating the key away from an
   entitled device, the holder check waits while an org the hub lists has no verdict yet (not verified, or verified
   behind the hub's `log_head`); a blocked org entitles nobody (fail closed, as the rotation slots already did).
   Holders of another epoch than the newest own `CK` are ignored (another browser's rotation is in flight). This
   closes the residual named in WEB-10 decision 10.
3. **A-07 – review follow-ups.** Records written by a holder the verified logs do not entitle are deleted like those
   of any device that lost eligibility (the browser never held that org's log, so it cannot check the author; blocking
   would leave the device the key until the user acted). A holder loss is handled once per device and sign-in,
   independent of the epoch: a blocked org entitles nobody here, but another browser that does not block it may wrap
   the next `CK` for its devices again; without the bound the two views would rotate against each other on every run.
   The check and the rotation use one snapshot of the verified logs; the hub reads the org list and the holders in one
   repeatable-read snapshot and caps the list at the contract's 10 000 items.
4. **A-07 – limits.** The holder list closes A-07 against an honest hub. A hub that leaves holders out, or reports a
   `log_head` beyond what it serves, can suppress the holder check (trust boundary: the hub may withhold); the
   log-based losses stay independent of it, and new wraps only ever go to readers of the verified logs. After a fresh
   sign-in the rotation can wait for the next run (only the first verified org's queue acts; at most the interval).
5. **Documentation for DOC-01.** 03-api.md §4.1 (`cred_holders` in `GET /v1/me`), 02-data-model.md §2.2 (`cred` wraps
   are kept on removal), 06-web.md §11 job 5 (the holder check), WEB-10 decision 10 (residual closed).
6. **A-08 – spec.** 06-web.md §10.2 (DOC-01 decision 4) already requires integrity for every chunk, fixed in
   `index.html` ("an import map with `integrity` … or `modulepreload` links with `integrity`"). The design below is the
   second variant with the hashes in `index.html` and the links created on demand; DOC-01 should name it so. Static
   `modulepreload` links for every chunk in `index.html` were rejected: the browser would download terminal, charts and
   Markdown on every first load (06-web.md §12.2 budget and TTI).
7. **A-08 – mechanism.** An import map with `integrity` would be the native way, but an inline
   `<script type="importmap">` falls under `script-src 'self'` of the hub's CSP (03-api.md §1) and would need a hash
   in the policy; external import maps do not exist. Instead:
   - the build writes `<meta name="asset-integrity" content="<path> <sha384-…> …">` with the hash of every script and
     stylesheet under `/assets/` into `index.html` (a meta element runs nothing; `index.html` is the anchor the
     published build hash covers, like the existing `integrity` attributes);
   - the bundler's preload helper (`\0vite/preload-helper.js`, through which every dynamic `import()` passes the list
     of files the chunk needs: itself, its static imports and its CSS) is replaced by `web/src/core/assets/integrity.ts`
     (build plugin `lazyIntegrity` in `web/build/sri.ts`). Before the import it loads each file through
     `<link rel="modulepreload"|"stylesheet" integrity crossorigin="anonymous">` and **waits** for it (Vite's own helper
     does not wait for module preloads, so the import could fetch unchecked bytes first); the browser keeps the checked
     module in its module map, so the import runs exactly those bytes. A file without a hash or with other bytes
     rejects the import (fail closed; the failure is kept for the page).
   - The helper always checks the imported chunk itself, read from the import function's source
     (`() => import("./X.js")`): the bundler emits an empty list for a chunk that needs nothing but itself. An import
     function whose target it cannot read is refused. A failed file is checked again on the next import (a browser
     keeps a failed module failed until reload; a stylesheet can recover); the screen shows the error page with reload.
   - The build (`writeBundle`) and `npm run build:check` fail when the table is missing, duplicated, incomplete or
     stale; when the in-repo helper is missing or the bundler's own helper is present; for every `import()` that is not
     a literal chunk path passed with its dependency list (chained `.then(…)` allowed), that names a file the build did
     not write, or whose chunk statically imports a file neither the first load nor the list checks.
   - `index.html` lists the module preloads and the stylesheet before the entry script (also checked): otherwise a hub
     that stalls the page after the script tag could have the browser fetch the start chunks' static imports without
     integrity (review finding on the existing WEB-01 order).
   - Not covered by a browser test: a changed lazily loaded stylesheet (unit-tested; the CSS chunks belong to screens
     that need a signed-in device world) and browsers other than Chromium (module map reuse of a module preload is
     specified HTML behaviour; Firefox/Safari belong on the manual release checklist of 06-web.md §12.7 – DOC-01).
   - Browser test `web/e2e/lazy-integrity.spec.ts`: the security screen's chunk changed on the way is refused, its code
     does not run; red without the plugin.
   The new module lives in `web/src/core/assets/` (new directory, no other owner); the replaced helper depends on
   Vite's internal module ID, which is pinned by the lock file and guarded by the build check.
8. **A-16 – owner from the signed author.** The task list shows as owner the author of the task's first verified
   `task.meta` in `scope_seq` order whose author the verified org state knows as a user (the event `POST /v1/tasks`
   sends with the task); device-authored `task.meta` (`task.set_title`) are skipped, an unknown author ends the search
   (no owner rather than a later one). Not ordered by the signed `ts`: its writer chooses it, so a writer of the
   workgroup could backdate a `task.meta` of someone else's task and appear as owner (review finding). With an honest
   hub the result equals the hub's `owner_id`; the hub's field is no longer read for display (`selectOwner` in
   `web/src/core/tasks/directory.ts`, hook `useOwner`). The list already pulls the `task.meta` events for the titles,
   so no extra request. Until a signed `task.meta` is known the row shows the neutral "Member" label. Residual: a hub
   that withholds or reorders the creator's `task.meta` can show another writer who also wrote one as owner – a
   withholding/ordering attack of the hub (trust boundary), never a forged author.
