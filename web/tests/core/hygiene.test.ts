// Web hygiene (07-testing.md section 3, 06-web.md sections 2.2 and 13): no coverage exclusions anywhere in the web
// project, no test-only code paths in src/, and a framework-free src/core.
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../helpers';

const SKIP = new Set(['node_modules', 'test-results', 'playwright-report', '.probe']);
const CODE = /\.(?:ts|tsx|js|mjs|cjs)$/;

function files(dir: string): string[] {
  return readdirSync(join(WEB_DIR, dir), { withFileTypes: true }).flatMap((entry) => {
    const path = `${dir}/${entry.name}`;
    if (entry.isDirectory()) return SKIP.has(entry.name) ? [] : files(path);
    return CODE.test(entry.name) ? [path] : [];
  });
}

const read = (path: string) => readFileSync(join(WEB_DIR, path), 'utf8');
// Built at run time so that this file does not match itself.
const EXCLUSION = new RegExp(['(?:v8|c8|istanbul)', 'ignore'].join('\\s+'), 'i');
const MODE = new RegExp(['import\\.meta\\.env', 'MODE'].join('\\.'));
const MARKER = ['__', 'TEST', '__'].join('');

describe('web hygiene', () => {
  const all = ['src', 'tests', 'e2e', 'build', 'scripts'].flatMap(files);

  it('scans the whole project', () => {
    expect(all).toEqual(expect.arrayContaining(['src/main.tsx', 'e2e/fixtures/test.ts', 'build/sri.ts']));
  });

  it('has no coverage exclusions', () => {
    expect(all.filter((path) => EXCLUSION.test(read(path)))).toEqual([]);
  });

  it('has no test-only code paths in src', () => {
    expect(files('src').filter((path) => MODE.test(read(path)) || read(path).includes(MARKER))).toEqual([]);
  });

  it('keeps src/core free of React and the UI', () => {
    const offending = files('src/core').filter((path) => /from '(?:react[^']*|[^']*\/ui\/[^']*)'/.test(read(path)));
    expect(offending).toEqual([]);
  });
});
