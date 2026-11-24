// The cross-system build comparison of the Linux part of the gates (06-web.md section 10.3, decision HOT-18): two
// build manifests of the same commit, one built on macOS and one in the Linux VM below another directory.
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { buildHash, manifestDifferences } from '../../scripts/build-compare.mjs';
import { WEB_DIR } from '../helpers';

const manifest = (files: { path: string; sha256: string; size: number }[], commit = 'a'.repeat(40)) =>
  `${JSON.stringify({ version: '0.1.0', commit, files, root_key_fingerprint: 'f' }, null, 2)}\n`;

const A = { path: 'assets/start-A.js', sha256: '1', size: 1 };
const B = { path: 'assets/screens-B.js', sha256: '2', size: 2 };

describe('build comparison', () => {
  it('finds no difference between equal manifests and hashes them like the build', () => {
    const text = manifest([A, B]);
    expect(manifestDifferences(text, text)).toEqual([]);
    expect(buildHash(text)).toMatch(/^[0-9a-f]{64}$/);
    expect(buildHash(text)).not.toBe(buildHash(manifest([A])));
  });

  it('names files only one side has, files that differ and other manifest fields', () => {
    const left = manifest([A, B]);
    const right = manifest(
      [
        { ...A, sha256: '9', size: 3 },
        { path: 'assets/index-C.js', sha256: '3', size: 3 },
      ],
      'b'.repeat(40),
    );
    expect(manifestDifferences(left, right)).toEqual([
      'assets/start-A.js: content differs (sha256 1 / 9, size 1 / 3)',
      'assets/screens-B.js: only in the first build',
      'assets/index-C.js: only in the second build',
      'commit differs',
    ]);
  });

  it('reports a difference of the bytes alone (formatting of the manifest)', () => {
    expect(manifestDifferences(manifest([A]), manifest([A]).trimEnd())).toEqual(['manifest bytes differ']);
  });
});

describe('build comparison command', () => {
  const script = join(WEB_DIR, 'scripts', 'build-compare.mjs');
  const run = (...args: string[]) => spawnSync(process.execPath, [script, ...args], { encoding: 'utf8' });

  function files(...texts: string[]): string[] {
    const base = join(WEB_DIR, 'node_modules', '.tmp');
    mkdirSync(base, { recursive: true });
    const dir = mkdtempSync(join(base, 'build-compare-'));
    return texts.map((text, index) => {
      const path = join(dir, `${index}.json`);
      writeFileSync(path, text);
      return path;
    });
  }

  it('exits 0 with the hash when the manifests are equal and 1 with the files when not', () => {
    const [same, other] = files(manifest([A]), manifest([B]));
    const equal = run('compare', same!, same!);
    expect([equal.status, equal.stdout]).toEqual([0, `build hashes equal: ${buildHash(manifest([A]))}\n`]);
    const differ = run('compare', same!, other!);
    expect(differ.status).toBe(1);
    expect(differ.stderr).toContain('assets/start-A.js: only in the first build');
  });

  it('refuses other arguments', () => {
    expect(run('compare', 'one').status).toBe(2);
    expect(run().stderr).toContain('usage: build-compare.mjs build OUT_DIR | compare MANIFEST MANIFEST');
  });
});
