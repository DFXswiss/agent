# HOT-16 – decisions

Package: the first screen was on its time budget (06-web.md section 12.2). `web/e2e/performance.spec.ts` measured a
median of 2933 to 3109 ms against 3000 ms on the merge train with no change to the web code, so measurement noise
decided every second run. Two stages: a temporary tolerance, then a smaller first load.

1. **Stage 1: temporary tolerance (removed in stage 2).** The time check failed only above 3000 ms × 1.05 (3150 ms).
   Between 3000 and 3150 ms it passed with an "over budget within tolerance" annotation and warning line. The budget
   itself stayed 3 s. Stage 2 removes the tolerance: the check is strict 3000 ms again.
2. **Screens load lazily (replaces the lazy-loading part of WEB-13 decision 10).** Every screen except sign-in, the
   start redirect and not-found is its own chunk (`ui/lazy/screen.tsx`, `lazyScreen` in each `routes.tsx`). WEB-13
   gave two reasons against lazy screens, and both are now resolved:
   - SRI: every lazy chunk is integrity-checked since KOR-04.
   - Coverage alignment of route chunks: decided by the merged coverage gate of this package's gate run (see the
     report).

   The element of a route stays a component, so registrations and tests keep their shape. Vitest loads every lazy
   screen before the tests (`tests/ui/setup.ts`, `preloadScreens`), so screen tests still render synchronously. The
   small registry behind `preloadScreens` ships in the bundle; it costs a few bytes and keeps the tests off the module
   internals.
3. **Frame while loading, failure inside the shell.** The screen area shows `screen.loading` at once (a busy status
   with a hidden "Loading…" and three placeholder bars, never an empty page). The header, navigation and footer of the
   shell stay, because the layout is in the first load. This also holds on the first client-side switch to the screen
   (Vitest test with navigation, Playwright `e2e/lazy-screen.spec.ts` with a held chunk). A chunk that cannot be
   fetched or fails its integrity check (`ChunkUnavailable`) is caught by the existing `ChunkBoundary` (ACC-FINAL
   decision 11). In place of the screen, it shows "This page could not be loaded" with a reload (`screen.unavailable`,
   `error.reload`). The connection banner stays visible, instead of the generic `ui_error` page replacing the shell and
   pointing to support. Other errors still reach the route's error page. A browser keeps a failed module as failed
   until reload, so the failure is cached and nothing is retried. The security screen uses the same mechanism instead
   of React Router's `route.lazy`, so all lazy screens behave the same. A direct link into a lazy screen now waits for
   one more chunk after the first load; the first screen, sign-in, does not.
4. **Chunk groups by reachability, not by share count.** The groups `core` and `shared` (`minShareCount: 2`) gathered
   every module that two chunks shared. With lazy screens, those were mostly modules shared between screens, so the
   whole group was loaded at start (231 KiB instead of 262 KiB, little gain). Without the groups the first load fell
   to 199 KiB, but in 16 scripts, which means a cascade of requests on a slow link. The groups are replaced by one
   group `start` with rolldown's built-in tag `$initial`: the app modules imported statically from the entry. The
   first load is now three scripts (framework, start, runtime) with 191.8 KiB gzip. The E2E time check finds the entry
   script through `index.html` instead of the name `index-`.
5. **Hooks used by the header moved out of their screens.** The bell imported `useOpen` from `NotificationsScreen`
   and the header entry imported `useInviter` from `PlatformInvitesScreen`, which kept both whole screens (and, through
   them, device and member parts) in the first load. The hooks now live in `notifications/useOpen.ts` and
   `platform-invites/useInviter.ts`. Their behaviour is unchanged.
6. **Build check: an import back from the importer.** The bundler may place code that a lazy chunk and its own
   importer share inside the importer (for example, the uPlot chart chunk imports from the monitoring screen chunk).
   `dynamicImportProblems` demanded such a chunk in the import's dependency list. The importing chunk and its static
   imports are already loaded and checked when its code runs, so they no longer need to be listed. A chunk imported
   from the first load still needs every unchecked dependency listed (test in `tests/build/lazy-sri.test.ts`).
7. **`INITIAL_JS_BUDGET` = 211 KiB.** This is the measured 191.8 KiB plus 10 %, fixed in `web/build/budget.ts` and
   explained in 06-web.md section 12.2. The 300 KiB ceiling of the specification stays. The guard is the value the
   build check, the build tests and the E2E budget test enforce.
8. **Message catalogs stay in the first load (not split per page and language).** They are about 40 KiB gzip for both
   languages, roughly half of it for areas the first load already uses. Loading only the shown language would add a
   request round trip (about 0.56 s on "Fast 3G") after the scripts, which is slower than downloading the second
   language. Splitting by area would need a maintained list of start areas, a completeness check and asynchronous
   catalog loading on locale change. That is more behaviour risk than this urgent package should take now that both
   targets are met with margin: 191.8 KiB of at most 240 KiB, and a median of about 2440 to 2480 ms of at most
   2600 ms. It is the next lever if the first load grows again.
