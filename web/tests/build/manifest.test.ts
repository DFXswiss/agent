import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  compiledRootKey,
  decodeB64u,
  listFiles,
  MANIFEST_FILE,
  rootKeyFingerprint,
  sha256Hex,
  TEST_ROOT_KEY as SHIPPED_TEST_KEY,
  writeManifest,
} from '../../build/manifest.ts';
import { WEB_DIR } from '../helpers';

const TEST_ROOT_KEY = '4es19ejjw83HvBQaTcmCqzLKLe7OQffXqDUsvsMeYkE';

function scratch() {
  const base = join(WEB_DIR, 'node_modules', '.tmp');
  mkdirSync(base, { recursive: true });
  const dir = mkdtempSync(join(base, 'manifest-'));
  mkdirSync(join(dir, 'assets'));
  writeFileSync(join(dir, 'index.html'), '<html></html>');
  writeFileSync(join(dir, 'assets', 'b.js'), 'b');
  writeFileSync(join(dir, 'assets', 'a.css'), 'a');
  writeFileSync(join(dir, '.keep'), '');
  writeFileSync(join(dir, MANIFEST_FILE), 'old');
  return dir;
}

describe('build manifest', () => {
  it('compiles the configured key, else the test key', () => {
    expect(SHIPPED_TEST_KEY).toBe(TEST_ROOT_KEY);
    expect(compiledRootKey({ VITE_PLATFORM_ROOT_KEY: 'X' })).toBe('X');
    expect(compiledRootKey({ VITE_PLATFORM_ROOT_KEY: '' })).toBe(TEST_ROOT_KEY);
    expect(compiledRootKey({})).toBe(TEST_ROOT_KEY);
    expect(typeof compiledRootKey()).toBe('string');
  });

  it('decodes only canonical base64url', () => {
    expect([...decodeB64u('AAE')]).toEqual([0, 1]);
    expect(() => decodeB64u('AAE=')).toThrow('not canonical');
    expect(() => decodeB64u('A')).toThrow('not canonical');
    expect(() => decodeB64u('AAF')).toThrow('not canonical');
  });

  it('fingerprints the 32-byte root key', () => {
    expect(rootKeyFingerprint(TEST_ROOT_KEY)).toBe('56421351fa344415ee7d8f52e455612e3d469d4968851f0aba15b4ffd810e912');
    expect(() => rootKeyFingerprint('')).toThrow('32 bytes');
    expect(() => rootKeyFingerprint('AAE')).toThrow('32 bytes');
    expect(() => rootKeyFingerprint('A+==')).toThrow('32 bytes');
  });

  it('lists files sorted without .keep and the manifest itself', () => {
    expect(listFiles(scratch())).toEqual([
      { path: 'assets/a.css', sha256: sha256Hex('a'), size: 1 },
      { path: 'assets/b.js', sha256: sha256Hex('b'), size: 1 },
      { path: 'index.html', sha256: sha256Hex('<html></html>'), size: 13 },
    ]);
  });

  it('writes the manifest in the specified field order and returns the build hash', () => {
    const dir = scratch();
    const hash = writeManifest(dir, { version: '1.2.3', commit: 'abc', rootKey: TEST_ROOT_KEY });
    const text = readFileSync(join(dir, MANIFEST_FILE), 'utf8');
    expect(hash).toBe(sha256Hex(text));
    expect(Object.keys(JSON.parse(text))).toEqual(['version', 'commit', 'files', 'root_key_fingerprint']);
    expect(text.endsWith('}\n')).toBe(true);
  });
});
