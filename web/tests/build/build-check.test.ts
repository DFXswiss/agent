import { cpSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { writeManifest } from '../../build/manifest.ts';
import { addAssetIntegrity, addIntegrity, listAssets, readFrom } from '../../build/sri.ts';
import {
  absolutePathProblems,
  bundleProblems,
  check,
  compareTrees,
  manifestProblems,
} from '../../scripts/build-check.mjs';
import { releaseProblems } from '../../scripts/build.mjs';
import { WEB_DIR } from '../helpers';

const ROOT_KEY = '4es19ejjw83HvBQaTcmCqzLKLe7OQffXqDUsvsMeYkE';
const COMMIT = 'a'.repeat(40);

function output(js = 'export const a = `cannot tell which chunk the import loads`;') {
  const base = join(WEB_DIR, 'node_modules', '.tmp');
  mkdirSync(base, { recursive: true });
  const dir = mkdtempSync(join(base, 'build-check-'));
  mkdirSync(join(dir, 'assets'));
  writeFileSync(join(dir, 'assets', 'a.js'), js);
  writeFileSync(join(dir, 'assets', 'a.js.map'), '{"__TEST__":1}');
  const html = '<html><head><script type="module" src="/assets/a.js"></script></head><body></body></html>';
  const sealed = addAssetIntegrity(addIntegrity(html, readFrom(dir)), listAssets(dir), readFrom(dir));
  writeFileSync(join(dir, 'index.html'), sealed);
  writeManifest(dir, { version: '1.0.0', commit: COMMIT, rootKey: ROOT_KEY });
  return dir;
}

describe('build check', () => {
  it('accepts a clean output (source maps are not delivered code)', () => {
    expect(check(output(), ROOT_KEY)).toEqual([]);
  });

  it('refuses lazily loaded code without integrity (KOR-04)', () => {
    const dir = output('export const a = 1; import("./b.js");');
    writeFileSync(join(dir, 'assets', 'b.js'), 'export const b = 2;');
    expect(check(dir, ROOT_KEY)).toEqual([
      '<meta name="asset-integrity">: hashes missing or wrong',
      'the integrity-checking preload helper is missing (dynamic imports would load unchecked code)',
      '/assets/a.js: an import() that does not go through the integrity check',
      'manifest files differ from output',
    ]);
  });

  it('compares two outputs byte by byte', () => {
    const a = output();
    const b = mkdtempSync(join(WEB_DIR, 'node_modules', '.tmp', 'build-check-copy-'));
    cpSync(a, b, { recursive: true });
    expect(compareTrees(a, b)).toEqual([]);
    writeFileSync(join(b, 'assets', 'a.js'), 'changed');
    writeFileSync(join(b, 'extra.js'), '');
    writeManifest(b, { version: '1.0.0', commit: COMMIT, rootKey: ROOT_KEY });
    expect(compareTrees(a, b)).toEqual([
      'not reproducible: assets/a.js',
      'not reproducible: extra.js',
      'not reproducible: build-manifest.json',
    ]);
  });

  it('finds test markers and global assignments', () => {
    const dir = output(
      'window.hook = 1; if (globalThis.x == 1) self.y=() => 1; window.z === 2; window["k"] = 1; globalThis.n ??= 1;' +
        ' Object.defineProperty(window, "d", {}); "__TEST__";',
    );
    expect(bundleProblems(dir)).toEqual([
      'assets/a.js: contains __TEST__',
      'assets/a.js: assigns a global (window.hook =)',
      'assets/a.js: assigns a global (self.y=)',
      'assets/a.js: assigns a global (window["k"] =)',
      'assets/a.js: assigns a global (globalThis.n ??=)',
      'assets/a.js: assigns a global (Object.defineProperty(window)',
    ]);
  });

  it('checks the manifest against the output and the key', () => {
    const dir = output();
    writeFileSync(join(dir, 'late.txt'), 'x');
    expect(manifestProblems(dir, 'A'.repeat(43))).toEqual([
      'manifest files differ from output',
      'manifest root key fingerprint',
    ]);
    const bad = output();
    writeManifest(bad, { version: '', commit: 'unknown', rootKey: ROOT_KEY });
    expect(manifestProblems(bad, ROOT_KEY)).toEqual([
      'manifest commit is not a commit hash',
      'manifest version missing',
    ]);
  });
});

describe('absolute paths and release builds', () => {
  it('finds machine paths in any output file, source maps included', () => {
    const dir = output();
    expect(absolutePathProblems(dir, ['/build/root'])).toEqual([]);
    writeFileSync(join(dir, 'assets', 'a.js.map'), '{"sources":["/build/root/web/src/a.ts"]}');
    expect(absolutePathProblems(dir, ['/build/root', ''])).toEqual([
      'assets/a.js.map: contains an absolute path of the build machine',
    ]);
    expect(absolutePathProblems(output())).toEqual([]);
  });

  it('refuses the test key, a missing key and a dirty tree for a release', () => {
    const clean = () => '';
    expect(releaseProblems({ VITE_PLATFORM_ROOT_KEY: 'A'.repeat(43) }, clean)).toEqual([]);
    expect(releaseProblems({}, () => ' M x')).toEqual([
      'VITE_PLATFORM_ROOT_KEY must be set to the production platform root key',
      'the working tree has uncommitted changes',
    ]);
    expect(releaseProblems({ VITE_PLATFORM_ROOT_KEY: ROOT_KEY }, clean)).toHaveLength(1);
    expect(typeof releaseProblems({})).toBe('object');
  });
});
