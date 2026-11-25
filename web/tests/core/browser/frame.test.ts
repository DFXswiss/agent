// Opening a live frame of the watched session with the workgroup key of its epoch (the vector frame of
// `contracts/vectors/browserframe.json`): the picture, a frame of another session, and no key of the epoch.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { frameOpener } from '../../../src/core/browser/frame';
import { CryptoError } from '../../../src/core/crypto/errors';
import { Secret } from '../../../src/core/crypto/secret';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { epochKey, WG } from '../../../src/core/log/state';
import { RequestError } from '../../../src/core/requests/client';
import { createStore } from '../../../src/core/store';
import { addKeys, ensureOrg } from '../../../src/core/store/slices/orgs';
import { WEB_DIR } from '../../helpers';
import { ORG } from './world';

const vector = JSON.parse(readFileSync(join(WEB_DIR, '../contracts/vectors/browserframe.json'), 'utf8')) as {
  frame: Record<string, unknown>;
  workgroup_key: string;
  jpeg: string;
  session: string;
  wg: string;
  epoch: number;
  browser: string;
  seq: number;
  ts: string;
};

function store(withKey: boolean) {
  const created = createStore();
  ensureOrg(created, ORG);
  if (withKey) {
    const key = Secret.of(b64uDecode(vector.workgroup_key));
    addKeys(created, ORG, new Map([[epochKey(WG, vector.wg, vector.epoch), key]]));
  }
  return created;
}

describe('frameOpener', () => {
  it('opens the vector frame with the key of its epoch', async () => {
    const open = frameOpener(store(true), ORG, vector.wg, vector.session);
    expect(await open(vector.frame)).toEqual({
      browser: vector.browser,
      seq: vector.seq,
      ts: Date.parse(vector.ts),
      jpeg: b64uDecode(vector.jpeg),
    });
  });

  it('refuses a frame of another session and one without a key of its epoch', async () => {
    const other = frameOpener(store(true), ORG, vector.wg, '01930000-0000-7000-8000-0000000000c9');
    await expect(other(vector.frame)).rejects.toBeInstanceOf(CryptoError);
    const keyless = frameOpener(store(false), ORG, vector.wg, vector.session);
    await expect(keyless(vector.frame)).rejects.toEqual(new RequestError('keys_pending'));
  });
});
