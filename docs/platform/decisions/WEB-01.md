# WEB-01 – decisions

Web scaffold: Vite/React/Tailwind, registration points, i18n with the error catalog, CSP/SRI, reproducible build,
merged coverage gate (06-web.md §2.1–§2.5, §10.1–§10.4, §13; 07-testing.md §1, §3, §8; ADR-019; ADR-027 §4, §6).

1. **Versions (all pinned exactly).** React 18.3.1, React Router 6.30.6 (data router), Tailwind 3.4.19, Vite 8.3.8,
   Vitest 5.1.1, Playwright 1.65.1, monocart-coverage-reports 3.2.1, ESLint 10, Prettier 3, TypeScript 6.0.3 (the
   newest version typescript-eslint supports). xterm is pinned to **5.5.0** with `addon-webgl` 0.18.0, `addon-fit`
   0.10.0 and `addon-canvas` 0.7.0, because xterm 6 has no canvas addon any more and 06 §2.1 requires the canvas
   fallback. No runtime dependency beyond 06 §2.1.
2. **Additional dev dependencies:** `jsdom` (environment of the UI unit tests), `@testing-library/{dom,jest-dom,
   user-event}`, `@vitejs/plugin-react`, `autoprefixer`/`postcss` (Tailwind build), typescript-eslint, React hooks
   rules, `eslint-config-prettier`, `globals`, `@types/*`, and `vitest-monocart-coverage` (see 4).
3. **Lint/format** follow the DFX `app` repository: Prettier `singleQuote`, `trailingComma: all`, `printWidth 120`;
   typescript-eslint recommended with unused names only behind `_`; `--max-warnings 0`. Added: React hooks rules for
   `src/` and `tests/`, a rule against `import.meta.env.MODE` and a rule keeping `src/core` free of React and `ui/`.
4. **Coverage.** `npm run test:coverage` collects with `@vitest/coverage-v8` through the `vitest-monocart-coverage`
   provider: Vitest's own thresholds enforce 100 % (lines, branches, functions, statements) for `src/core/**` alone,
   and the raw V8 data of all of `src/` is kept. `npm run test:e2e` (Playwright) adds browser V8 data; its global
   teardown merges both with monocart and fails below 100 % lines or branches, and also when a file of `src/` has no
   coverage data at all. Order of the gate: `test:coverage` before `test:e2e` (Makefile, workflow).
5. **Coverage data location:** `web/node_modules/.coverage/` instead of `web/coverage/`. The raw V8 files hold
   absolute paths; the repository hygiene scan (`tests/platform/test_hygiene.py`, owned by FND-00) skips
   `node_modules` but not `coverage`, so this keeps the scan green without editing a foreign file.
6. **E2E build unminified.** The Playwright suite builds the app with `minify: false` (otherwise identical to
   `npm run build`: same plugins, SRI, CSP, hidden source maps). Mapping V8 ranges of minified code back to the
   sources yields branch ranges that do not line up with the Vitest ranges and break the merge (measured: 25 %
   branches merged against 100 % in each source). `make platform-web` ends with the minified production build.
   Source rule that follows from the merge: no `cond ? x : undefined` (the bundler rewrites `undefined` to `void 0`
   and the branch ranges differ); write an `if` or use `null`.
7. **Source maps** are `hidden`: emitted next to the assets (listed in the manifest, `sources` relative to `web/`, so
   independent of the output location) without a `sourceMappingURL` comment in the delivered code. The coverage
   fixture attaches them from disk.
8. **Window globals.** React Router 6 assigns `window.__reactRouterVersion`; the build plugin `web/build/globals.ts`
   removes exactly that statement. `build:check` scans the delivered JavaScript for global assignments and
   `__TEST__`; a Playwright spec compares the own properties of `window` against a page without the bundle.
9. **SRI** is computed in `writeBundle` from the bytes on disk (after every other plugin ran) and the same module
   checks index.html: no inline script/style, no `style`/`on*` attributes, no `javascript:`, every script, stylesheet
   and module preload from `/assets/` with a matching `sha384` and `crossorigin="anonymous"`. The module-preload
   polyfill is off and `assetsInlineLimit` is 0 (the CSP allows no `data:` fonts or scripts).
10. **`build-manifest.json`** = `{version, commit, files[{path, sha256, size}], root_key_fingerprint}` over every
    output file except itself and `.keep`; `root_key_fingerprint` = SHA-256 (hex) of the 32 key bytes; the build hash
    is the SHA-256 of the manifest file. `version` from `web/package.json`, `commit` = `git rev-parse HEAD`,
    `SOURCE_DATE_EPOCH` = commit time (all overridable by the environment). `.keep` is re-emitted because Vite empties
    the output directory.
11. **`build:check`** builds twice into `web/node_modules/.tmp/build-check/{a,b}` and compares every file, then
    applies the index.html, bundle and manifest checks to the result and prints the build hash. `make platform-web`
    runs it; `platform-web.yml` now runs the full gate (no skip any more).
12. **Platform root key.** The build compiles `VITE_PLATFORM_ROOT_KEY` from the environment (no `.env` file: the
    root `.gitignore` ignores `.env`, and Vite's env-file loading is off). Without it, the TEST key
    `TEST_ROOT_KEY` of `web/build/manifest.ts` is compiled in: the Ed25519 public key of the public seed
    `SHA-256("agent.dfx.swiss test platform root key")`, `4es19ejjw83HvBQaTcmCqzLKLe7OQffXqDUsvsMeYkE`
    (fingerprint `56421351…d810e912`); test hubs and E2E runs derive the private key from the same seed. The build
    prints a note when the test key is compiled in, and `npm run build -- --release` refuses the test key, a missing
    key and a dirty working tree (the manifest records HEAD). Every build refuses a key that is not 32 bytes of
    canonical b64u, and the manifest shows which key was compiled in.
13. **Configuration check** (`src/core/config/check.ts`): `GET /v1/config` without credentials and cache; network
    error, non-2xx, non-JSON or a body without the fields the client relies on → `unavailable`; another
    `platform_root_key` (or no compiled key) → `platform_root_mismatch`; `origin` ≠ `location.origin` or `rp_id` ≠ its
    host name → `origin_mismatch` (an existing catalog code). The basic error page shows title, body, action and the
    code from the catalog, with a reload button.
14. **Rendering errors** of a screen show the generic error page with the client-side code `ui_error` (not a hub code,
    so the catalog's generic text with the code is shown, as 06 §2.5 prescribes for unknown codes).
15. **Error catalog source.** `contracts/enums/` holds codes only, so `web/scripts/gen-errors.mjs` combines the code
    lists (`error-codes`, `operation-error-codes`, `session-failure-reasons`, the scheduling reasons of `task-states`;
    105 codes) with the texts in `web/scripts/error-texts.json`. It fails on a code without `{title, body, action}` in
    both locales and on texts for unknown codes; `--check` (also run by a Vitest test) fails on stale catalogs. A
    contract package that adds a code adds its texts in the same change. German texts use Swiss orthography (`ss`).
16. **Message catalogs** `catalog/<area>.<locale>.json` are flat; keys are addressed as `<area>.<key>` so areas cannot
    collide. Missing keys fall back to English, then to the key. Unknown placeholders stay visible.
17. **Registration points** (ADR-027 §4) are literal `import.meta.glob` calls with pure collectors in
    `src/core/registry.ts` (sorted by module path; `ordered()` by `order`, then `id`; duplicate IDs and malformed
    exports throw). Exports: screens `routes: RouteObject[]`, navigation `nav: NavEntry[]`, settings sections
    `section`, task tabs `tab`, chat items `chatItem` (file name = kind), header items `headerItem`, store slices
    `slice`, jobs `job` (file name = name). The app shell hooks in through `src/ui/shell/layout.tsx` (export `Layout`
    rendering an `<Outlet />`), so WEB-03 adds a file instead of editing the router. Store slices and jobs have
    `index.ts` files in `src/core/store/slices/` and `src/core/jobs/`; WEB-02 extends the registration types there.
    Each glob is proven by a test that checks the literal pattern, the real (mostly empty) result and the collector
    with fixture modules under `web/tests/fixtures/registry/` discovered by the same glob shape.
18. **Hub test server.** HUB-02 was merged while this package was open, so the Playwright suite runs against the
    real hub: `scripts/hub_test_server.py` creates a throwaway database with fresh owner/writer roles on
    `AGENT_HUB_TEST_POSTGRES` (pattern of `tests/platform/hub/conftest.py`), migrates it, runs `python -m agent_hub
    serve` on `WP_PORT_BASE+2` (else `WEB_E2E_PORT`, else 8790) with `AGENT_HUB_PUBLIC_URL=http://localhost:<port>`
    and the compiled platform root key, and drops database and roles when stopped. The hub serves the build from
    `src/agent_hub/static/` with its own CSP and cache headers; the specs compare them with 03-api §1
    (`web/e2e/fixtures/headers.ts`). Python: `WEB_E2E_PYTHON`, else the repository `.venv`, else `python3`.
    `platform-web.yml` therefore gets a PostgreSQL 16 service and `pip install -e ".[platform]"`. The Vite preview
    server with the same headers (`web/e2e/fixtures/server.ts`) remains only for the probe page (port + 1), which
    the hub does not serve.
19. **CSP style-injection proof.** The probe page `web/e2e/probe/` (built separately, served under `/probe/`) mounts
    a read-only xterm terminal with the WebGL and with the canvas renderer and a uPlot chart under the hub CSP. A
    MutationObserver installed before any script records `<style>` insertions. Result: **no runtime `<style>`
    element and no CSP violation** with xterm 5.5 (WebGL and canvas addon loaded before `open()`) and uPlot 1.6;
    ADR-019 needs no amendment. Screens must load the renderer addon before `terminal.open()` and import
    `@xterm/xterm/css/xterm.css` and `uplot/dist/uPlot.min.css` as static CSS.
20. **CSP listener fixture** (`web/e2e/fixtures/test.ts`, auto fixture `cspViolations`): an init script forwards every
    `securitypolicyviolation` of every page through a Playwright binding; the test fails if any is recorded. A spec
    proves the listener catches an inline `<style>` and an inline `<script>`. Listener and coverage apply to the
    default `context` and to every context opened with the `newContext` fixture; specs never call
    `browser.newContext()` directly.
21. **Root `.gitignore`** ignores every `build/` directory; two negations (`!web/build/`, `!web/tests/build/`) keep the
    build plugins and their tests tracked.
22. **`build:check` also refuses** absolute paths of the build machine (repository root, home directory) in any
    output file including source maps, and global assignments in the forms `x.y =`, `x["y"] =`, `x.y ??=` and
    `Object.defineProperty(window, …)` (`x` = `window`/`globalThis`/`self`). SRI also covers `rel="preload"`.
23. **Review decisions.** Kept: source maps stay in the served output (hidden, open-source content, part of the build
    hash, needed to map coverage; decision 7). Kept: the merged gate reads the Vitest raw data of the preceding
    `test:coverage` run (Makefile and workflow run both in order; a failed Vitest run deletes the raw data, so the
    gate then stops with "missing"). The gate compares exact covered/total ratios, not rounded percentages.
24. **Fix in a HUB-02 test.** `tests/platform/hub/core/test_static_headers.py::test_without_a_build_the_page_is_unavailable`
    used the package static directory and failed as soon as a web build existed there (every `make platform-ci` after
    the first web build). It now starts its hub on an empty temporary static directory; the assertions are unchanged.
