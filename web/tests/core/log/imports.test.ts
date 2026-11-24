// 04-crypto.md section 1 and 06-web.md section 2.2 for the log core: src/core/log imports only modules of src/core
// (WebCrypto through crypto/, no crypto library, no Node API, no framework) and does not read the build environment:
// the app passes the compiled platform root key (`VITE_PLATFORM_ROOT_KEY`) in, as for the start-up config check.
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../../helpers';

const DIR = join(WEB_DIR, 'src/core/log');
const sources = readdirSync(DIR).map((name) => join(DIR, name));
const specifiers = (text: string) => [...text.matchAll(/(?:from|import)\s*\(?\s*'([^']+)'/g)].map((m) => m[1]!);

describe('log core dependencies', () => {
  it('has the modules of the package', () => {
    expect(readdirSync(DIR).sort()).toEqual([
      'catchup.ts',
      'entries.ts',
      'events.ts',
      'platform.ts',
      'rotation.ts',
      'state.ts',
      'verify.ts',
      'wraps.ts',
    ]);
  });

  it('imports only its own modules and the encoding and crypto core', () => {
    const offending = sources.flatMap((path) =>
      specifiers(readFileSync(path, 'utf8'))
        .filter((spec) => !/^\.\/[a-z]+$/.test(spec) && !/^\.\.\/(?:encoding|crypto)\/[a-z0-9]+$/.test(spec))
        .map((spec) => `${path}: ${spec}`),
    );
    expect(offending).toEqual([]);
  });

  it('uses no Node crypto, no Math.random, no network and no build environment', () => {
    const offending = sources.filter((path) =>
      /node:|\brequire\(|Math\.random|\bfetch\(|import\.meta/.test(readFileSync(path, 'utf8')),
    );
    expect(offending).toEqual([]);
  });
});
