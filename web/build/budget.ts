// Performance budget (06-web.md section 12.2): the JavaScript a first visit loads before any lazy chunk – the entry
// scripts of index.html (`<script src>`, `<link rel="modulepreload">`) and every module they import statically,
// followed transitively – after gzip (level 6, what servers and proxies use by default). Lazily loaded chunks
// (`import("…")`: screens, terminal, charts, Markdown) do not count.
import { readFileSync } from 'node:fs';
import { dirname, join, posix } from 'node:path';
import { gzipSync } from 'node:zlib';
import { parseAttributes } from './sri.ts';

/**
 * The guard of the build: the measured first load (191.8 KiB gzip, HOT-16) plus 10 %, well below the 300 KiB ceiling of
 * section 12.2, so every package sees growth of the first load in its own gates. Raise it only with a reason there.
 */
export const INITIAL_JS_BUDGET = 211 * 1024;

const TAG_RE = /<(script|link)\b([^>]*)>/gi;
// `import … from "x"`, `import "x"` and `export … from "x"` (also right after a comment) – never `import("x")`
const STATIC_IMPORT_RE = /(?:^|[;\n}/])\s*(?:import|export)\s*(?:[\w${},*\s]+?\s*from\s*)?["']([^"']+)["']/g;

/** URLs of the scripts index.html loads at start (entry scripts and module preloads). */
export function entryScripts(html: string): string[] {
  const urls: string[] = [];
  for (const match of html.matchAll(TAG_RE)) {
    const tag = match[1]!.toLowerCase();
    const attributes = parseAttributes(match[2]!);
    if (tag === 'script' && attributes.has('src')) urls.push(attributes.get('src')!);
    if (tag === 'link' && attributes.get('rel') === 'modulepreload') urls.push(attributes.get('href')!);
  }
  return urls;
}

/** Relative static imports of a module's code (absolute or bare specifiers are not files of the build). */
export function staticImports(code: string): string[] {
  return [...code.matchAll(STATIC_IMPORT_RE)].map((match) => match[1]!).filter((spec) => spec.startsWith('.'));
}

export interface InitialScript {
  readonly path: string;
  readonly bytes: number;
  readonly gzip: number;
}

/** Every script of the first load of the build in `dir`, with its size and gzip size. */
export function initialScripts(dir: string): InitialScript[] {
  const seen = new Map<string, InitialScript>();
  const pending = entryScripts(readFileSync(join(dir, 'index.html'), 'utf8')).map((url) => url.replace(/^\//, ''));
  while (pending.length > 0) {
    const path = posix.normalize(pending.pop()!);
    if (seen.has(path)) continue;
    const code = readFileSync(join(dir, path));
    seen.set(path, { path, bytes: code.length, gzip: gzipSync(code, { level: 6 }).length });
    for (const spec of staticImports(code.toString('utf8'))) pending.push(posix.join(dirname(path), spec));
  }
  return [...seen.values()].sort((a, b) => (a.path < b.path ? -1 : 1));
}

/** Problems of the build in `dir` with the budget (none if the initial JS fits). */
export function budgetProblems(dir: string, budget = INITIAL_JS_BUDGET): string[] {
  const scripts = initialScripts(dir);
  const total = scripts.reduce((sum, script) => sum + script.gzip, 0);
  if (total <= budget) return [];
  const list = scripts.map((script) => `${script.path} ${(script.gzip / 1024).toFixed(1)} KiB`).join(', ');
  return [`initial JS ${(total / 1024).toFixed(1)} KiB gzip exceeds ${budget / 1024} KiB (${list})`];
}
