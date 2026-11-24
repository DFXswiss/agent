// Job `github-rewrap` (ADR-076 section 1): own further GitHub records under an older CK epoch are re-sealed under the
// newest one, only in the queue of the first verified org, serialized with the credential re-wrap, nothing after a
// sign-out; registered after the credential re-wrap on sign-in and the interval.
import { describe, expect, it } from 'vitest';
import { bodyJson } from '../../../src/core/encoding/json';
import { newHeader, sealCredential } from '../../../src/core/crypto/credential';
import { Secret } from '../../../src/core/crypto/secret';
import { backgroundJobs, type JobContext } from '../../../src/core/jobs';
import { job, rewrapGithub } from '../../../src/core/jobs/github-rewrap';
import { TS } from '../log/world';
import { credWorld, type CredWorld, selfWrap } from '../credentials/world';

const ID = '0190f000-0000-7000-8000-0000000000e2';
const BODY = { token: 'gho_x', login: 'octo-cat', user_id: 1, scopes: [], created_at: TS };

function jobContext(t: CredWorld): JobContext {
  return {
    store: t.store,
    client: t.client,
    rootKey: t.platform.root,
    now: () => t.clock.now,
    persist: {
      loadPinned: async () => null,
      savePinned: async () => undefined,
      loadPlatformHead: async () => null,
      savePlatformHead: async () => undefined,
    },
  };
}

async function world() {
  const t = await credWorld();
  t.hub.wraps.push(await selfWrap(t, 2, Secret.of(new Uint8Array(32).fill(8)), null));
  const header = newHeader({ id: ID, owner: t.me.id, kind: 'github', version: 1, epoch: 1, author: t.me.id, ts: TS });
  const record = await sealCredential(t.credKey, header, bodyJson(BODY), t.me.identity.sig.privateKey);
  t.client.on('GET /v1/me/github-identities', () => ({
    identities: [
      {
        id: ID,
        owner_id: t.me.id,
        owner_kind: 'user',
        primary: false,
        version: 1,
        epoch: 1,
        created_at: TS,
        updated_at: TS,
      },
    ],
    defaults: [],
  }));
  t.client.on(`GET /v1/github-identities/${ID}`, () => ({
    owner_id: t.me.id,
    owner_kind: 'user',
    primary: false,
    record,
  }));
  t.client.on(`PUT /v1/github-identities/${ID}`, () => ({ version: 2 }));
  return t;
}

describe('github-rewrap', () => {
  it('re-seals in the queue of the first verified org only', async () => {
    const t = await world();
    const run = { org: t.org, trigger: 'login' as const, generation: t.store.generation };
    expect(await rewrapGithub(jobContext(t), run)).toBe(1);
    expect(await rewrapGithub(jobContext(t), { ...run, org: 'another-org' })).toBeNull();
    await job.run(jobContext(t), run);
    expect(t.client.of(`PUT /v1/github-identities/${ID}`)).toHaveLength(2);
  });

  it('does nothing after a sign-out', async () => {
    const t = await world();
    const run = { org: t.org, trigger: 'interval' as const, generation: t.store.generation - 1 };
    expect(await rewrapGithub(jobContext(t), run)).toBe(0);
    expect(t.client.of('GET /v1/me/github-identities')).toHaveLength(0);
  });

  it('is registered after the credential re-wrap on sign-in and the interval', () => {
    expect(job).toMatchObject({ name: 'github-rewrap', order: 36, triggers: ['login', 'interval'] });
    const names = backgroundJobs().map((item) => item.name);
    expect(names.indexOf('github-rewrap')).toBeGreaterThan(names.indexOf('credential-rewrap'));
  });
});
