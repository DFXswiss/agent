# WEB-13 – decisions

Smartphone, accessibility, connectivity, error pages and the security page (06-web.md sections 2.4, 2.5, 10.3–10.5,
12, 13; 04-crypto.md sections 7 and 9; ADR-019, ADR-027).

1. **axe-core as a dev dependency (ADR-027 section 6).** The automated accessibility check of section 12.3 runs
   `@axe-core/playwright` 4.14.0 (pinned exactly, it pulls `axe-core` ~4.14) inside the Playwright suite on every
   route. It is a test-only dependency: it is never imported from `src/` and not part of the build. The check fails on
   violations of impact `serious` or `critical` (WCAG 2.x A/AA rules and axe best practices); `minor`/`moderate`
   findings are reported in the test output only.
2. **Build manifest served by the hub.** The security page computes the build hash as SHA-256 of the exact bytes of
   `build-manifest.json`. The hub delivered only `index.html` and `/assets/`, so `agent_hub/static_routes.py` (the
   delivery of the web build, HUB-02) also serves `GET /build-manifest.json` unchanged with `Cache-Control: no-cache`
   (404 `not_found` without a build), before its SPA catch-all. A new area router was tried first, but areas are
   pinned in the skeleton test and every area route must be an API route of `contracts/api/INDEX.json`; this small
   addition to the static delivery is the only change outside the owned paths in the hub. The page
   also computes the fingerprint of the compiled platform root key (the checked `platform_root_key` of
   `GET /v1/config`, equal to the compiled key since section 10.4) and flags a manifest that names another key. It
   is linked from a footer on every page (also anonymous) and from the "More" sheet.
3. **Responsive shell.** Below `md` a fixed bottom navigation (Home, Tasks, Notifications, Monitoring, More) replaces
   the navigation row; "Tasks" opens the workgroup in view or the active org's default workgroup (from `GET /v1/me`);
   "More" is a sheet with the remaining discovered entries, settings and the security page. From `md` to `lg` the
   discovered entries are a row under the header; from `lg` the same `<nav>` is a left sidebar that also lists the
   readable workgroups of the active org (the org list stays in the header switcher). Test IDs `shell.nav.*` are
   unchanged. The bottom bar shows only with an active org; anonymous pages have the footer only.
4. **Sheets and dialogs.** `ui/responsive/Sheet.tsx` is the modal of all screens (full screen below `md`, centred card
   from `md`), with the focus trap of `ui/a11y/focus.ts`; `screens/members/Dialog.tsx` re-exports it under its old
   name so its users stay unchanged.
5. **On-screen keyboard.** Phones shrink the visual viewport, not the layout viewport. `ui/responsive/keyboard.ts`
   measures the covered part (≥ 80 px counts as a keyboard), the shell hides the bottom bar and pads the page by it,
   and both composers scroll themselves into view while focused (`useAboveKeyboard`). Verified by Vitest with a fake
   `visualViewport`; Chromium emulation cannot open a keyboard.
6. **Pinch-zoom (deviation from section 12.1).** "Pinch-zoom allowed on the terminal container only" cannot be had
   with CSS: `touch-action` is intersected up the ancestor chain, so disabling pinch on the page disables it on the
   terminal too, and a browser pinch zooms the whole visual viewport anyway. Disabling page zoom would also hurt
   low-vision users (WCAG 1.4.4). Pinch-zoom therefore stays allowed on every page (the viewport allows zoom); the
   terminal keeps its fixed geometry and scrolls horizontally inside its own container, so zooming never reflows it.
   A terminal-local pinch gesture (changing the font size) is left to a later package if wanted.
7. **Error pages (section 12.6).** `screens/errors/` has the common frame (`ErrorLayout`) and the pages: "Not
   available" for `not_found` and `forbidden` alike (a page never reveals whether something exists; used by the task
   and session screens and the platform invitations), "Session expired" with a new passkey sign-in that loads the
   screen again (`useRenewal` extracted from the renewal banner), other load errors with catalog text, code and "Try
   again", and the unsupported-browser page with the missing features and the supported browsers (moved from
   `shell/`). `ErrorPage` (blocking start errors such as `503 unavailable`, `platform_root_mismatch`), `BlockedOrg`
   and `NotFound` use the same frame; their test IDs are unchanged.
8. **Connection announcements.** The visible banner counts the seconds to the next attempt; announcing that text
   would read out every second. The banner is no live region any more; a separate polite live region
   (`ui/a11y/LiveRegion.tsx`) announces each change of the state.
9. **Accessibility checks.** The route sweeps (`e2e/responsive.spec.ts` anonymous routes and blocking error pages,
   `e2e/devices/responsive.spec.ts` every signed-in route, every task and session tab, the invitation dialog and the
   "More" sheet) run on the desktop project and on Pixel 7 and iPhone 15 (Chromium emulation). Touch targets: every
   visible interactive element (an input together with its labels) is at least 44 × 44 CSS px; links inside the text
   of a Markdown block are inline targets (WCAG 2.5.8 inline exception) and exempt. The sweep of signed-in routes needs
   the device double, so it lives in `e2e/devices/` (not `e2e/responsive.spec.ts` as the package text names it); the
   phone projects of device specs run after the `devices` project (`dependencies`), one device spec at a time.
   Amended by HOT-21: each phone's device project runs in a lane with a hub of its own, at the same time as `devices`
   (`decisions/HOT-21.md`).
10. **Performance (section 12.2).** Measured on the production build (minified), not on the unminified coverage build.
    `npm run build:check` fails above 300 KiB gzip of initial JavaScript (entry scripts, module preloads and their
    static imports, `build/budget.ts`); currently about 215 KiB. The TTI test (`e2e/performance.spec.ts`) builds the
    production bundle, serves it gzip compressed with the hub's headers (the hub itself does not compress; the edge in
    front of it does), and measures navigation start → enabled sign-in button with CPU ×4 and DevTools "Fast 3G"
    (median of three loads ≤ 3 s; measured about 2.65 s, before 3.3 s). The decisive change: `index.html` preloads
    `GET /v1/config` (`<link rel="preload" as="fetch" crossorigin="anonymous">`), so the configuration no longer waits
    for the scripts (one round trip of ~0.56 s). For the preload to be used, `core/config/check.ts` fetches with
    `credentials: 'same-origin'` and the default cache mode instead of `omit`/`no-store`; the hub has no cookies and
    answers `no-store`, so nothing is sent or cached differently (verified: one `/v1/config` request per load).
    `build/sri.ts` allows a data preload (`as="fetch"`) without integrity only for `/v1/` paths; scripts and styles keep
    SRI. The chunks are grouped (`framework`, shared `core`, shared `ui`), so the first load is a few larger requests;
    the bundler's runtime chunk is left out of the browser coverage (no app source).
    **Lazy loading (deviation for the settings).** Terminal, charts and Markdown load lazily (as before). Lazy routes
    for the screens and for the settings were tried: they did not change the TTI noticeably once the configuration was
    preloaded, they take code out of SRI (later chunks carry no integrity; the review rated that high for the code that
    handles invitation codes, PRF outputs and recovery codes), and the merged coverage gate (`07-testing.md` section 3)
    does not line up the Vitest ranges of modules in lazily loaded route chunks with the browser's, so already covered
    branches of those files counted as uncovered. The settings therefore stay in the start chunks; section 12.2's
    purpose (initial JS ≤ 300 KiB, TTI ≤ 3 s) is met and checked.
11. **Times (section 12.4).** All event times already used `formatEventTime`; the terminal history position showed only
    the recording offset. Chunks now carry their header time (`Chunk.atMs`), and the position is also shown as absolute
    local time (`absoluteAt`: the header time of the first chunk reaching the offset, written at its last frame). The
    header time is set when the runner creates the chunk event (normally about 100 ms after the last frame, later
    after a backlog), so the shown time is the best the client knows, not a recorder clock.
12. **i18n (section 2.5).** The parity test of all catalog pairs existed (WEB-01). New: the ESLint rule
    `i18n/no-literal-text` (`web/eslint/i18n.js`) for `src/ui/**/*.tsx` reports JSX text and shown attributes
    (`aria-label`, `title`, `placeholder`, `alt`, `label`, …) with a letter; it found one literal ("s" after the
    elapsed seconds of a countdown step), now a catalog text.
13. **Small fixes found by the sweeps.** Touch targets of the audit row links, the monitoring task link, the workgroup
    name link and the PR draft checkbox label; stale monitoring rows greyed with `text-gray-600` (AA contrast on the
    state badge); Markdown wraps long words and its code blocks scroll inside a focusable box; long tool summaries
    break; the toast stacks keep inside the viewport on phones.
14. **Phone projects.** The phone projects run a fixed list of web specs (`MOBILE_SPECS` in `playwright.config.ts`):
    the WEB-13 sweeps cover every route on the phones, and running all component specs three times would triple the
    suite. The full-chain web scenarios in the mobile project are SCN-08's (`e2e/platform`, project `mobile`).
15. **Review follow-ups.** The error page titles keep their level-1 heading (the alert role is on a wrapper); the
    "More" sheet is rendered through a portal (above toasts, outside the bar's stacking context) and its button has
    `aria-expanded`; the skip link moves the focus without changing the URL fragment (the invitation code is read from
    it); the GitHub device-code expiry is announced once per started minute, not every second; the budget scanner also
    follows an import right after a comment.
