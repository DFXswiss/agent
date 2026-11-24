// The cross-system build comparison of the Linux part of the gates (06-web.md section 10.3, decision HOT-18): two
// build manifests of the same commit, one built on macOS and one in the Linux VM below another directory.
import { describe, expect, it } from 'vitest';
import { buildHash, manifestDifferences } from '../../scripts/build-compare.mjs';

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
