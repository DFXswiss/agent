# ADR-019: Web stack, CSP and reproducible build

Status: Accepted (2026-11-21)

## Context
- Legacy hub UI: static HTML with inline scripts, no CSP (`PASSKEY-PROTOCOL.md` §10.1 asks for CSP).
- Spike D: WebCrypto core proven byte-identical with Python; recommendation TypeScript strict + Vite + React 18 + Tailwind 3 (consistent with the DFX `app`), Vitest + Playwright with merged V8 coverage via monocart.
- For web E2EE the hub serving JavaScript is the largest residual risk.

## Decision
- `agent-core/web/`: TypeScript strict, Vite, React 18, Tailwind 3, npm, Node 24; Prettier/ESLint configuration as in `app` (`--max-warnings 0`); `@xterm/xterm` for the terminal; no JS crypto dependency.
- Structure "pure core, thin shell": `web/src/core/**` (crypto, log verification, sync, protocol; framework-free, runs in Node) and `web/src/ui/**`.
- Built assets served by the hub under `/assets/` with the CSP of `03-api.md` §1 (no inline script/style, `connect-src 'self'`), SRI on every script and stylesheet, content-hashed filenames.
- Terminal rendering with `@xterm/addon-webgl` (fallback `@xterm/addon-canvas`) because the default DOM renderer injects `<style>` elements; uPlot and xterm CSS are imported as static files. M0 verifies that no runtime `<style>` injection remains. **Amendment rule:** if it does, the only permitted relaxation is `style-src-elem 'self' 'unsafe-inline'`, recorded as an amendment of this ADR; `script-src` stays `'self'` without exceptions.
- Reproducible build (pinned lockfile, `SOURCE_DATE_EPOCH`); the build hash is published with each release and shown on the "Security" page; platform root key compiled in.
- Coverage: Vitest alone 100 % for `core/`; Vitest + Playwright merged 100 % lines and branches for all of `web/src`.

## Consequences
- Node is needed for build and test only, not at runtime.
- Malicious JavaScript from a compromised hub remains possible; documented to users.

## Alternatives considered
Keep vanilla static pages (no testable component structure, inline scripts), CRA (deprecated), a native app (later option for code pinning).

## Deviation from DESIGN.md
None.
