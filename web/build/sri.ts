// Subresource integrity for index.html (06-web.md section 10.2, ADR-019): every `<script src>` and every
// `<link rel="stylesheet"|"modulepreload">` gets `integrity="sha384-…"` and `crossorigin="anonymous"`, computed from
// the bytes written to disk. The same module checks that index.html has no inline script or style, no style or
// event-handler attributes and no external resources; the build fails otherwise.
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, posix } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';

export type ReadAsset = (url: string) => Uint8Array;

const TAG_RE = /<(script|link)\b([^>]*)>/gi;
const ANY_TAG_RE = /<([a-zA-Z][a-zA-Z0-9-]*)\b([^>]*)>/g;
const ATTR_RE = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
const INTEGRITY_RELS = new Set(['stylesheet', 'modulepreload', 'preload']);

export function sriHash(bytes: Uint8Array): string {
  return `sha384-${createHash('sha384').update(bytes).digest('base64')}`;
}

export function parseAttributes(text: string): Map<string, string> {
  const attributes = new Map<string, string>();
  for (const match of text.matchAll(ATTR_RE)) {
    attributes.set(match[1]!.toLowerCase(), match[2] ?? match[3] ?? match[4] ?? '');
  }
  return attributes;
}

/** A preload of API data (`as="fetch"`), not of code: it needs no integrity but must stay on the hub's `/v1/` API. */
export function isDataPreload(tag: string, attributes: Map<string, string>): boolean {
  return tag === 'link' && attributes.get('rel') === 'preload' && attributes.get('as') === 'fetch';
}

/** URL of the resource that needs integrity, or null for tags without one (e.g. `<link rel="icon">`). */
export function integrityUrl(tag: string, attributes: Map<string, string>): string | null {
  if (tag === 'script') return attributes.get('src') ?? null;
  if (isDataPreload(tag, attributes)) return null;
  if (tag === 'link' && INTEGRITY_RELS.has(attributes.get('rel') ?? '')) return attributes.get('href') ?? null;
  return null;
}

function serialize(tag: string, attributes: Map<string, string>, selfClosing: boolean): string {
  const parts = [...attributes].map(
    ([name, value]) => `${name}="${value.replace(/&/g, '&amp;').replace(/"/g, '&quot;')}"`,
  );
  return `<${tag} ${parts.join(' ')}${selfClosing ? ' /' : ''}>`;
}

const SCRIPT_TAG_RE = /[ \t]*<script\b[^>]*\bsrc=[^>]*><\/script>\n?/gi;

/**
 * index.html with the external scripts moved to the end of the head, after the module preloads and stylesheets:
 * the browser fetches the static imports of a module script without integrity unless a preload with integrity already
 * claimed them, so a hub that stalls the rest of the page after the script tag could otherwise feed it unchecked
 * start chunks (KOR-04).
 */
export function scriptsLast(html: string): string {
  const scripts = html.match(SCRIPT_TAG_RE) ?? [];
  if (scripts.length === 0 || !/<\/head>/i.test(html)) return html;
  const without = html.replace(SCRIPT_TAG_RE, '');
  const block = scripts.map((tag) => `    ${tag.trim()}\n`).join('');
  return without.replace(/[ \t]*<\/head>/i, (head) => `${block}${head}`);
}

/** index.html with integrity and crossorigin on every script, stylesheet and module preload (scripts last). */
export function addIntegrity(html: string, read: ReadAsset): string {
  return scriptsLast(addIntegrityAttributes(html, read));
}

function addIntegrityAttributes(html: string, read: ReadAsset): string {
  return html.replace(TAG_RE, (whole, tagName: string, body: string) => {
    const tag = tagName.toLowerCase();
    const selfClosing = body.trimEnd().endsWith('/');
    const attributes = parseAttributes(selfClosing ? body.trimEnd().slice(0, -1) : body);
    const url = integrityUrl(tag, attributes);
    if (url === null || !url.startsWith('/assets/')) return whole;
    attributes.delete('integrity');
    attributes.delete('crossorigin');
    attributes.set('integrity', sriHash(read(url)));
    attributes.set('crossorigin', 'anonymous');
    return serialize(tag, attributes, selfClosing);
  });
}

/** Every CSP/SRI rule index.html violates (empty when the page is fine). */
export function htmlProblems(html: string, read: ReadAsset): string[] {
  const problems: string[] = [];
  let scriptSeen = false;
  if (/<script\b[^>]*>\s*[^<\s]/i.test(html)) problems.push('inline script');
  if (/javascript:/i.test(html)) problems.push('javascript: URL');
  for (const match of html.matchAll(ANY_TAG_RE)) {
    const tag = match[1]!.toLowerCase();
    const attributes = parseAttributes(match[2]!.replace(/\/\s*$/, ''));
    if (tag === 'style') problems.push('inline <style> element');
    if (tag === 'script' && !attributes.has('src')) problems.push('script without src');
    for (const name of attributes.keys()) {
      if (name === 'style' || name.startsWith('on')) problems.push(`<${tag}> with attribute ${name}`);
    }
    const data = isDataPreload(tag, attributes) ? (attributes.get('href') ?? '') : null;
    if (data !== null && !/^\/v1\/[a-z]/.test(data)) problems.push(`<${tag}> preloads ${data} outside /v1/`);
    const url = integrityUrl(tag, attributes);
    if (url === null) continue;
    if (tag === 'script') scriptSeen = true;
    else if (scriptSeen) problems.push(`<${tag}> ${url} comes after a script (preloads and stylesheets first)`);
    if (!url.startsWith('/assets/')) {
      problems.push(`<${tag}> loads ${url} from outside /assets/`);
      continue;
    }
    if (attributes.get('integrity') !== sriHash(read(url)))
      problems.push(`<${tag}> ${url}: integrity missing or wrong`);
    if (attributes.get('crossorigin') !== 'anonymous') problems.push(`<${tag}> ${url}: crossorigin must be anonymous`);
  }
  return problems;
}

export const readFrom =
  (dir: string): ReadAsset =>
  (url) =>
    readFileSync(join(dir, url));

/** Vite plugin: rewrites the written index.html with SRI attributes and fails the build on any problem. */
export function sri(): Plugin {
  return {
    name: 'agent-platform-sri',
    apply: 'build',
    enforce: 'post',
    writeBundle: {
      sequential: true,
      order: 'post',
      handler(options) {
        const dir = options.dir!;
        const file = join(dir, 'index.html');
        const assets = listAssets(dir);
        const html = addAssetIntegrity(addIntegrity(readFileSync(file, 'utf8'), readFrom(dir)), assets, readFrom(dir));
        const problems = [
          ...htmlProblems(html, readFrom(dir)),
          ...assetIntegrityProblems(html, assets, readFrom(dir)),
          ...dynamicImportProblems(html, assets, readFrom(dir)),
        ];
        if (problems.length) throw new Error(`index.html violates the CSP/SRI rules:\n  ${problems.join('\n  ')}`);
        writeFileSync(file, html);
      },
    },
  };
}

/** The id under which the bundler loads its preload helper for dynamic imports. */
export const PRELOAD_HELPER_ID = '\0vite/preload-helper.js';
/** The in-repo helper that checks the integrity of everything a dynamic import loads. */
export const INTEGRITY_HELPER = fileURLToPath(new URL('../src/core/assets/integrity.ts', import.meta.url));

/** Vite plugin: dynamic imports go through `src/core/assets/integrity.ts` instead of the bundler's own helper. */
export function lazyIntegrity(): Plugin {
  return {
    name: 'agent-platform-lazy-integrity',
    apply: 'build',
    enforce: 'pre',
    load(id) {
      if (id !== PRELOAD_HELPER_ID) return null;
      return `export { __vitePreload } from ${JSON.stringify(INTEGRITY_HELPER)};`;
    },
  };
}

/** The name of the meta element with the hashes of every asset (`src/core/assets/integrity.ts`). */
export const INTEGRITY_META = 'asset-integrity';
const META_RE = /\s*<meta\s+name="asset-integrity"\s+content="([^"]*)"\s*\/?>/g;
const IMPORT_CALL_RE = /\bimport\(/g;
const LITERAL_IMPORT_RE = /^import\(\s*(["'`])(\.\/[^"'`]+)\1\s*\)/;
const CHAINED_CALL_RE = /^\s*\.\s*[\w$]+\s*\(/;
const DEPS_RE = /^\s*,\s*(?:__vite__mapDeps\(\[([\d,]*)\]\)|\[\]|void 0)/;
const FILE_DEPS_RE = /\b__vite__mapDeps=[^;]*?\.f=(\[[^\]]*\])/;
const STATIC_IMPORT_RE = /(?:^|[;\n}/])\s*(?:import|export)\s*(?:[\w${},*\s]+?\s*from\s*)?["'](\.[^"']+)["']/g;
/** Texts of the in-repo helper and of the bundler's own one (which must not be in the bundle). */
const OWN_HELPER = 'cannot tell which chunk the import loads';
const BUNDLER_HELPER = 'Unable to preload CSS';

/** The index just after the parenthesis that closes the one at `open` (string literals skipped), or -1. */
export function closingParen(code: string, open: number): number {
  let depth = 0;
  for (let index = open; index < code.length; index += 1) {
    const char = code[index]!;
    if (char === '"' || char === "'" || char === '`') {
      const end = code.indexOf(char, index + 1);
      if (end < 0) return -1;
      index = end;
    } else if (char === '(') depth += 1;
    else if (char === ')' && --depth === 0) return index + 1;
  }
  return -1;
}

/**
 * The chunk path and the dependency indexes of a checked import at the start of `code`:
 * `import("./X.js")[.then(…)…], <deps>` where `<deps>` is `__vite__mapDeps([…])`, `[]` or `void 0`; null otherwise.
 */
export function checkedImport(code: string): { path: string; deps: number[] } | null {
  const literal = LITERAL_IMPORT_RE.exec(code);
  if (literal === null) return null;
  let end = literal[0].length;
  let chained = CHAINED_CALL_RE.exec(code.slice(end));
  while (chained !== null) {
    end = closingParen(code, end + chained[0].length - 1);
    if (end < 0) return null;
    chained = CHAINED_CALL_RE.exec(code.slice(end));
  }
  const deps = DEPS_RE.exec(code.slice(end));
  if (deps === null) return null;
  return { path: literal[2]!, deps: (deps[1] ?? '').split(',').filter(Boolean).map(Number) };
}

/** The files the build writes under `/assets/` that run or style the page (`/assets/…`, sorted). */
export function listAssets(dir: string): string[] {
  return readdirSync(join(dir, 'assets'))
    .filter((name) => /\.(?:js|mjs|css|wasm)$/.test(name))
    .sort()
    .map((name) => `/assets/${name}`);
}

const tableOf = (assets: readonly string[], read: ReadAsset): string =>
  assets.map((url) => `${url} ${sriHash(read(url))}`).join(' ');

/** index.html with the meta element that names the hash of every asset (replacing an existing one). */
export function addAssetIntegrity(html: string, assets: readonly string[], read: ReadAsset): string {
  const meta = `<meta name="${INTEGRITY_META}" content="${tableOf(assets, read)}" />`;
  const without = html.replace(META_RE, '');
  return without.replace(/(\s*)<\/head>/i, (_whole, space: string) => `${space}  ${meta}${space}</head>`);
}

/** The asset table must exist once (any attribute order) and name exactly the assets with their current hashes. */
export function assetIntegrityProblems(html: string, assets: readonly string[], read: ReadAsset): string[] {
  const metas = [...html.matchAll(ANY_TAG_RE)].filter(
    (match) => match[1]!.toLowerCase() === 'meta' && parseAttributes(match[2]!).get('name') === INTEGRITY_META,
  );
  if (metas.length !== 1)
    return [`index.html needs exactly one <meta name="${INTEGRITY_META}"> (found ${metas.length})`];
  const found = parseAttributes(metas[0]![2]!.replace(/\/\s*$/, '')).get('content');
  return found === tableOf(assets, read) ? [] : [`<meta name="${INTEGRITY_META}">: hashes missing or wrong`];
}

const text = (read: ReadAsset, url: string): string => Buffer.from(read(url)).toString('utf8');

/** The static imports of a script, as `/assets/…` URLs (every chunk lives in `/assets/`). */
function importsOf(read: ReadAsset, url: string): string[] {
  return [...text(read, url).matchAll(STATIC_IMPORT_RE)].map((match) => posix.join(posix.dirname(url), match[1]!));
}

/** `roots` and everything they import statically. */
function closure(read: ReadAsset, roots: readonly string[]): Set<string> {
  const seen = new Set<string>();
  const pending = [...roots];
  while (pending.length > 0) {
    const url = pending.pop()!;
    if (seen.has(url)) continue;
    seen.add(url);
    pending.push(...importsOf(read, url));
  }
  return seen;
}

/** The scripts of the first load: those index.html loads with integrity and everything they import statically. */
function initialGraph(html: string, read: ReadAsset): Set<string> {
  const roots = [...html.matchAll(TAG_RE)]
    .map((match) => integrityUrl(match[1]!.toLowerCase(), parseAttributes(match[2]!)))
    .filter((url): url is string => url !== null && url.endsWith('.js'));
  return closure(read, roots);
}

/**
 * Every dynamic `import()` must be checked (`src/core/assets/integrity.ts`): a literal chunk path passed to the
 * in-repo preload helper together with a dependency list that names every static import of the chunk the first load
 * did not already check (the helper checks the chunk itself). The bundler's own helper must be gone. Anything else –
 * a non-literal `import()`, an unknown list format, a missing file – fails the build.
 */
export function dynamicImportProblems(html: string, assets: readonly string[], read: ReadAsset): string[] {
  const problems: string[] = [];
  const scripts = assets.filter((url) => /\.m?js$/.test(url));
  const all = scripts.map((url) => text(read, url));
  if (!all.some((code) => code.includes(OWN_HELPER))) {
    problems.push('the integrity-checking preload helper is missing (dynamic imports would load unchecked code)');
  }
  if (all.some((code) => code.includes(BUNDLER_HELPER))) {
    problems.push("the bundler's own preload helper is in the bundle (it loads chunks without integrity)");
  }
  const initial = initialGraph(html, read);
  scripts.forEach((url, index) => {
    const code = all[index]!;
    const fileDeps = FILE_DEPS_RE.exec(code);
    const files = fileDeps === null ? [] : (JSON.parse(fileDeps[1]!) as string[]).map((file) => `/${file}`);
    for (const call of code.matchAll(IMPORT_CALL_RE)) {
      const checked = checkedImport(code.slice(call.index));
      if (checked === null) {
        problems.push(`${url}: an import() that does not go through the integrity check`);
        continue;
      }
      const target = posix.join(posix.dirname(url), checked.path);
      const listed = new Set(checked.deps.map((i) => files[i]));
      if (!scripts.includes(target)) {
        problems.push(`${url}: import() of ${target}, which the build did not write`);
        continue;
      }
      for (const needed of closure(read, [target])) {
        if (needed !== target && !initial.has(needed) && !listed.has(needed))
          problems.push(`${url}: import() of ${target} would load ${needed} without integrity`);
      }
    }
  });
  return problems;
}
