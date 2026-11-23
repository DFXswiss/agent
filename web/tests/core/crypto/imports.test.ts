// 04-crypto.md section 1 ("Browser: WebCrypto only. No other crypto library") and 06-web.md section 2.2: the crypto
// core imports nothing but its own modules, never `node:crypto`, and the web project has no crypto dependency.
// The error codes of the core are the ones of the contracts (and so of the error catalog).
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { CRYPTO_CODES, LOG_CODES } from '../../../src/core/crypto/errors';
import { WEB_DIR } from '../../helpers';

const AREAS = ['encoding', 'crypto', 'passkey', 'tasks'];
const sources = AREAS.flatMap((area) =>
  readdirSync(join(WEB_DIR, 'src/core', area)).map((name) => `src/core/${area}/${name}`),
);
const read = (path: string) => readFileSync(join(WEB_DIR, path), 'utf8');
const specifiers = (text: string) => [...text.matchAll(/(?:from|import)\s*\(?\s*'([^']+)'/g)].map((m) => m[1]!);
// Packages that implement cryptography in JavaScript (or wrap it); none may appear in package.json.
const CRYPTO_PACKAGES =
  /^(?:@noble\/|@stablelib\/|@hpke\/|tweetnacl|libsodium|sodium|node-forge|crypto-js|elliptic|jose|jsrsasign|asmcrypto|sjcl|hash\.js|bn\.js|@peculiar\/|webcrypto|isomorphic-webcrypto|hpke)/;

describe('crypto core dependencies', () => {
  it('scans the four areas', () => {
    expect(sources).toEqual(expect.arrayContaining(['src/core/crypto/hpke.ts', 'src/core/tasks/title.ts']));
    expect(sources.every((path) => path.endsWith('.ts'))).toBe(true);
  });

  it('imports only relative modules of src/core', () => {
    const offending = sources.flatMap((path) =>
      specifiers(read(path))
        .filter((spec) => !spec.startsWith('./') && !(spec.startsWith('../') && !spec.startsWith('../../')))
        .map((spec) => `${path}: ${spec}`),
    );
    expect(offending).toEqual([]);
  });

  it('uses globalThis.crypto and never node:crypto or Math.random', () => {
    const offending = sources.filter((path) => /node:crypto|\brequire\(|Math\.random/.test(read(path)));
    expect(offending).toEqual([]);
  });

  it('has no JavaScript crypto package among the dependencies', () => {
    const pkg = JSON.parse(read('package.json'));
    const names = [...Object.keys(pkg.dependencies ?? {}), ...Object.keys(pkg.devDependencies ?? {})];
    expect(names.filter((name) => CRYPTO_PACKAGES.test(name))).toEqual([]);
  });
});

describe('error codes', () => {
  it('are codes of contracts/enums/error-codes.json', () => {
    const enums = JSON.parse(readFileSync(join(WEB_DIR, '..', 'contracts', 'enums', 'error-codes.json'), 'utf8'));
    const known = new Set(enums.codes.map((entry: { code: string }) => entry.code));
    expect([...CRYPTO_CODES, ...LOG_CODES].filter((code) => !known.has(code))).toEqual([]);
  });
});
