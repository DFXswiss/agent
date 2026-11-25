// The static completeness check of the volume gate (ADR-086 §4, KOR-35): every route pattern, task tab, chat item
// kind, settings section, header item and list id the web client registers must be named in the `COVERS` of a volume
// spec (`e2e/volume/*.spec.ts`); a new view, list or item kind without a volume check fails here.
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  covered,
  coversOf,
  discovered,
  listIds,
  readMeasured,
  routePatterns,
  runtimeGaps,
  seedFile,
  seedOverride,
  uncovered,
  unmeasured,
  unseedable,
} from '../../e2e/volume/completeness.ts';
import { WEB_DIR } from '../helpers';

function scratch(): string {
  const base = join(WEB_DIR, 'node_modules', '.tmp');
  mkdirSync(base, { recursive: true });
  return mkdtempSync(join(base, 'volume-'));
}

function write(dir: string, path: string, text: string): void {
  const full = join(dir, path);
  mkdirSync(join(full, '..'), { recursive: true });
  writeFileSync(full, text);
}

/** A minimal web tree: one screen with two routes, one tab, one chat item, one section, one header item. */
function tree(options: { lists?: string } = {}): string {
  const dir = scratch();
  write(dir, 'src/ui/screens/home/routes.tsx', "export const routes = [{ path: '/o/:org' }, { path: '/x' }];");
  write(dir, 'src/ui/screens/empty/other.tsx', '');
  write(dir, 'src/ui/task-tabs/chat/index.tsx', '');
  write(dir, 'src/ui/task-tabs/helpers.ts', '');
  write(dir, 'src/ui/chat-items/prompt.tsx', '');
  write(dir, 'src/ui/settings/sections/passkeys/index.tsx', '');
  write(dir, 'src/ui/shell/header/bell.tsx', '');
  if (options.lists !== undefined) write(dir, 'src/core/lists/ids.ts', options.lists);
  write(dir, 'e2e/volume/a.spec.ts', "export const COVERS = ['route:/o/:org', 'tab:chat'];");
  write(dir, 'e2e/volume/b.spec.ts', "export const COVERS: string[] = [\n  'route:/o/:org',\n  'chat:prompt',\n];");
  write(dir, 'e2e/volume/budget.ts', "export const COVERS = ['ignored'];");
  return dir;
}

describe('volume gate completeness', () => {
  it('names every registered view, tab, chat kind, settings section, header item and list in a volume spec', () => {
    const missing = uncovered(WEB_DIR);
    expect(missing, `ids without a volume spec (add them to the COVERS of e2e/volume/*.spec.ts)`).toEqual([]);
  });

  it('discovers the registrations of the web client (not an empty set)', () => {
    const ids = discovered(WEB_DIR);
    expect(ids).toContain('route:/o/:org/t/:task');
    expect(ids).toContain('tab:chat');
    expect(ids).toContain('chat:session.output');
    expect(ids).toContain('settings:passkeys');
    expect(ids).toContain('header:bell');
    expect(ids.filter((id) => id.startsWith('route:')).length).toBeGreaterThan(15);
  });

  it('reads the ids of a tree and its specs', () => {
    const dir = tree();
    expect(discovered(dir)).toEqual([
      'chat:prompt',
      'header:bell',
      'route:/o/:org',
      'route:/x',
      'settings:passkeys',
      'tab:chat',
    ]);
    expect([...covered(dir).entries()]).toEqual([
      ['route:/o/:org', ['a.spec.ts', 'b.spec.ts']],
      ['tab:chat', ['a.spec.ts']],
      ['chat:prompt', ['b.spec.ts']],
    ]);
    expect(uncovered(dir)).toEqual(['header:bell', 'route:/x', 'settings:passkeys']);
  });

  it('adds the list ids once src/core/lists/ids.ts exists', () => {
    const dir = tree({
      lists: "export const LIST_IDS = [\n  'task.chat',\n  'audit',\n] as const;\nconst x = 'string';",
    });
    expect(discovered(dir)).toContain('list:task.chat');
    expect(discovered(dir)).toContain('list:audit');
    expect(discovered(dir)).not.toContain('list:string');
    expect(() => listIds('export const OTHER = [];')).toThrow(/LIST_IDS/);
  });

  it('parses COVERS and route patterns from source text', () => {
    expect(coversOf("export const COVERS = ['a', 'b'];")).toEqual(['a', 'b']);
    expect(coversOf('const nothing = 1;')).toEqual([]);
    expect(routePatterns("[{ path: '/', element: x }, { path: '*' }]")).toEqual(['/', '*']);
  });

  it('lists every covered id not measured in both volume projects', () => {
    const dir = tree();
    expect(runtimeGaps(dir)).toEqual([]); // no seed in this run: nothing to check
    expect(unseedable(dir)).toEqual({});
    expect(seedOverride(dir)).toBeNull();
    expect(readMeasured(dir)).toEqual([]);
    write(dir, 'test-results/.volume/seed-volume.json', '{}');
    write(dir, 'test-results/.volume/other.json', 'not a seed');
    write(
      dir,
      'test-results/.volume/measured.jsonl',
      [
        { project: 'volume', id: 'route:/o/:org' },
        { project: 'volume-mobile', id: 'route:/o/:org' },
        { project: 'volume', id: 'tab:chat' },
      ]
        .map((item) => JSON.stringify(item))
        .join('\n') + '\n\n',
    );
    expect(runtimeGaps(dir)).toEqual(['volume chat:prompt', 'volume-mobile chat:prompt', 'volume-mobile tab:chat']);
    expect(unmeasured(dir, [])).toHaveLength(6);
    write(dir, 'test-results/.volume/seed-volume.json', JSON.stringify({ unseedable: { 'chat:prompt': 'refused' } }));
    write(dir, 'test-results/.volume/seed-volume-mobile.json', JSON.stringify({ unseedable: { 'tab:x': 'refused' } }));
    expect(unseedable(dir)).toEqual({ 'chat:prompt': 'refused', 'tab:x': 'refused' });
    write(dir, 'test-results/.volume/seed-volume-mobile.json', '{}');
    expect(unseedable(dir)).toEqual({ 'chat:prompt': 'refused' });
    expect(runtimeGaps(dir, { 'chat:prompt': 'known' })).toEqual(['volume-mobile tab:chat']);
    expect(runtimeGaps(dir)[0]).toMatch(/^chat:prompt: not seedable \(refused\) but not listed/);
    expect(runtimeGaps(dir, { 'chat:prompt': 'known', 'tab:chat': 'stale' })[0]).toMatch(/^tab:chat: listed/);
    write(dir, 'test-results/.volume/seed-volume.json', '{}');
    write(dir, 'test-results/.volume/seed-volume-mobile.json', JSON.stringify({ override: { tasks: 3 } }));
    expect(seedOverride(dir)).toEqual({ tasks: 3 });
    expect(runtimeGaps(dir)[0]).toMatch(/VOLUME_COUNTS shrank the data set/);
    write(dir, 'test-results/.volume/seed-volume-mobile.json', JSON.stringify({ override: {} }));
    expect(seedOverride(dir)).toBeNull();
    expect(seedFile(dir, 'volume')).toBe(join(dir, 'test-results', '.volume', 'seed-volume.json'));
  });
});
