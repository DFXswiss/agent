// Completeness of the volume gate (ADR-063 §4): every view, list and item kind the web client registers must be named
// in the `COVERS` of a volume spec (static check, `tests/scripts/volume-completeness.test.ts`), and every named id
// must have been measured in both volume projects (run-time check, `e2e/fixtures/global-teardown.ts`). The ids are
// discovered from the registration points (ADR-027 §4), never listed by hand:
// - `route:<pattern>`: the `path` of every route of `src/ui/screens/*/routes.tsx`;
// - `tab:<id>`: every task tab `src/ui/task-tabs/<id>/index.tsx`;
// - `chat:<kind>`: every chat item `src/ui/chat-items/<kind>.tsx`;
// - `settings:<id>`: every settings section `src/ui/settings/sections/<id>/index.tsx`;
// - `header:<name>`: every header item `src/ui/shell/header/<name>.tsx`;
// - `list:<id>`: every list id of `src/core/lists/ids.ts` (the closed set of ADR-063 §1).
// Nothing here imports the specs: `COVERS` is read from their source text, so Vitest checks it without Playwright.
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { basename, join } from 'node:path';

/** The volume specs (`e2e/volume/*.spec.ts`) below `webDir`. */
export function volumeSpecs(webDir: string): string[] {
  const dir = join(webDir, 'e2e', 'volume');
  return readdirSync(dir)
    .filter((name) => name.endsWith('.spec.ts'))
    .sort()
    .map((name) => join(dir, name));
}

/** The string literals of `export const COVERS = [...]` in a spec's source (empty without the declaration). */
export function coversOf(source: string): string[] {
  const match = /export const COVERS\s*(?::[^=]+)?=\s*\[([^\]]*)\]/.exec(source);
  if (!match) return [];
  return [...match[1]!.matchAll(/'([^']+)'/g)].map((item) => item[1]!);
}

/** Every id named by a volume spec, with the specs that name it. */
export function covered(webDir: string): Map<string, string[]> {
  const ids = new Map<string, string[]>();
  for (const spec of volumeSpecs(webDir)) {
    for (const id of coversOf(readFileSync(spec, 'utf8'))) ids.set(id, [...(ids.get(id) ?? []), basename(spec)]);
  }
  return ids;
}

const dirs = (path: string, file: string): string[] =>
  existsSync(path)
    ? readdirSync(path, { withFileTypes: true })
        .filter((entry) => entry.isDirectory() && existsSync(join(path, entry.name, file)))
        .map((entry) => entry.name)
    : [];

const files = (path: string, extension: string): string[] =>
  existsSync(path)
    ? readdirSync(path, { withFileTypes: true })
        .filter((entry) => entry.isFile() && entry.name.endsWith(extension))
        .map((entry) => entry.name.slice(0, -extension.length))
    : [];

/** The route patterns of one `routes.tsx` (every `path: '...'`). */
export function routePatterns(source: string): string[] {
  return [...source.matchAll(/\bpath:\s*'([^']+)'/g)].map((match) => match[1]!);
}

/** The list ids of `src/core/lists/ids.ts`: the quoted ids of its array `LIST_IDS = [...]` (the closed set). */
export function listIds(source: string): string[] {
  const match = /\bLIST_IDS\b[^=]*=\s*\[([^\]]*)\]/.exec(source);
  if (!match) throw new Error('src/core/lists/ids.ts: no `LIST_IDS = [...]`');
  return [...new Set([...match[1]!.matchAll(/'([^']+)'/g)].map((item) => item[1]!))];
}

/** Every id the web client registers today (sorted). */
export function discovered(webDir: string): string[] {
  const ui = join(webDir, 'src', 'ui');
  const ids: string[] = [];
  for (const screen of dirs(join(ui, 'screens'), 'routes.tsx')) {
    const source = readFileSync(join(ui, 'screens', screen, 'routes.tsx'), 'utf8');
    ids.push(...routePatterns(source).map((path) => `route:${path}`));
  }
  ids.push(...dirs(join(ui, 'task-tabs'), 'index.tsx').map((id) => `tab:${id}`));
  ids.push(...files(join(ui, 'chat-items'), '.tsx').map((kind) => `chat:${kind}`));
  ids.push(...dirs(join(ui, 'settings', 'sections'), 'index.tsx').map((id) => `settings:${id}`));
  ids.push(...files(join(ui, 'shell', 'header'), '.tsx').map((name) => `header:${name}`));
  // The closed list-id set arrives with the windowed lists of ADR-063 §1 (stage b of KOR-35); until that file exists
  // there are no list ids to check (the views that will carry them are covered by their route and tab ids).
  const lists = join(webDir, 'src', 'core', 'lists', 'ids.ts');
  if (existsSync(lists)) ids.push(...listIds(readFileSync(lists, 'utf8')).map((id) => `list:${id}`));
  return [...new Set(ids)].sort();
}

/** Registered ids that no volume spec names. */
export function uncovered(webDir: string): string[] {
  const named = covered(webDir);
  return discovered(webDir).filter((id) => !named.has(id));
}

/** The projects that must measure every covered id. */
export const VOLUME_PROJECTS = ['volume', 'volume-mobile'] as const;

export interface Measured {
  readonly project: string;
  readonly id: string;
}

/** Covered ids not measured in each volume project: `<project> <id>` lines (sorted). */
export function unmeasured(webDir: string, measured: readonly Measured[]): string[] {
  const seen = new Set(measured.map((item) => `${item.project} ${item.id}`));
  const missing: string[] = [];
  for (const id of covered(webDir).keys()) {
    for (const project of VOLUME_PROJECTS) if (!seen.has(`${project} ${id}`)) missing.push(`${project} ${id}`);
  }
  return missing.sort();
}

/** Run files of the volume suite (below Playwright's output directory, which every run starts empty). */
export const runDir = (webDir: string): string => join(webDir, 'test-results', '.volume');
export const seedFile = (webDir: string): string => join(runDir(webDir), 'seed.json');
export const measuredFile = (webDir: string): string => join(runDir(webDir), 'measured.jsonl');

/** The measurements of this run (one JSON object per line). */
export function readMeasured(webDir: string): Measured[] {
  const file = measuredFile(webDir);
  if (!existsSync(file)) return [];
  return readFileSync(file, 'utf8')
    .split('\n')
    .filter((line) => line.trim() !== '')
    .map((line) => JSON.parse(line) as Measured);
}

/**
 * The run-time check of the global teardown: when the volume data set was seeded in this run, every covered id must
 * have been measured in both volume projects; returns the missing `<project> <id>` lines (empty without a seed).
 */
export function runtimeGaps(webDir: string): string[] {
  if (!existsSync(seedFile(webDir))) return [];
  return unmeasured(webDir, readMeasured(webDir));
}
