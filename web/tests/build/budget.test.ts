// The performance budget of 06-web.md section 12.2 in the build check: entry scripts and module preloads of
// index.html, static imports followed transitively, dynamic imports left out, gzip sizes against the guard of 211 KiB.
import { randomBytes } from 'node:crypto';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { describe, expect, it } from 'vitest';
import { budgetProblems, entryScripts, INITIAL_JS_BUDGET, initialScripts, staticImports } from '../../build/budget.ts';
import { writeManifest } from '../../build/manifest.ts';
import { check } from '../../scripts/build-check.mjs';
import { WEB_DIR } from '../helpers';

function build(files: Record<string, string | Buffer>, html: string) {
  const base = join(WEB_DIR, 'node_modules', '.tmp');
  mkdirSync(base, { recursive: true });
  const dir = mkdtempSync(join(base, 'budget-'));
  mkdirSync(join(dir, 'assets', 'sub'), { recursive: true });
  for (const [path, content] of Object.entries(files)) writeFileSync(join(dir, path), content);
  writeFileSync(join(dir, 'index.html'), html);
  return dir;
}

describe('initial JS budget', () => {
  it('finds the entry scripts and module preloads of index.html', () => {
    const html =
      '<script type="module" src="/assets/a.js"></script><link rel="modulepreload" href="/assets/b.js">' +
      '<link rel="stylesheet" href="/assets/c.css"><script>inline()</script>';
    expect(entryScripts(html)).toEqual(['/assets/a.js', '/assets/b.js']);
  });

  it('follows static imports only', () => {
    const code =
      'import{a as b}from"./x.js";import "./y.js";\nexport{c}from"./sub/z.js";import"react";' +
      'const lazy=()=>import("./lazy.js");const s="import x from \'./no\'"/*banner*/import"./w.js";';
    expect(staticImports(code)).toEqual(['./x.js', './y.js', './sub/z.js', './w.js']);
  });

  it('sums the gzip size of the first load and refuses more than the guard', () => {
    const entry = 'import{x}from"./shared.js";import"./sub/deep.js";const l=()=>import("./lazy.js");';
    const dir = build(
      {
        'assets/entry.js': entry,
        'assets/shared.js': 'import"./entry.js";export const x=1;',
        'assets/sub/deep.js': 'import"../shared.js";',
        'assets/lazy.js': randomBytes(400 * 1024),
      },
      '<script type="module" src="/assets/entry.js"></script>',
    );
    const scripts = initialScripts(dir);
    expect(scripts.map((script) => script.path)).toEqual(['assets/entry.js', 'assets/shared.js', 'assets/sub/deep.js']);
    expect(scripts[0]).toEqual({
      path: 'assets/entry.js',
      bytes: entry.length,
      gzip: gzipSync(entry, { level: 6 }).length,
    });
    expect(budgetProblems(dir)).toEqual([]);
    const total = scripts.reduce((sum, script) => sum + script.gzip, 0);
    expect(budgetProblems(dir, total - 1)[0]).toMatch(/^initial JS .* KiB gzip exceeds .* KiB \(assets\/entry\.js/);
    expect(INITIAL_JS_BUDGET).toBe(211 * 1024);
  });

  it('is part of the build check', () => {
    const dir = build(
      { 'assets/big.js': randomBytes(INITIAL_JS_BUDGET + 1024) },
      '<script type="module" src="/assets/big.js"></script>',
    );
    writeManifest(dir, {
      version: '1.0.0',
      commit: 'a'.repeat(40),
      rootKey: '4es19ejjw83HvBQaTcmCqzLKLe7OQffXqDUsvsMeYkE',
    });
    expect(check(dir, '4es19ejjw83HvBQaTcmCqzLKLe7OQffXqDUsvsMeYkE').join('\n')).toMatch(/exceeds 211 KiB/);
  });
});
