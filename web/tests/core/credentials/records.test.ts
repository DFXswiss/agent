// U-WEB-credentials (WEB-10): credential records built under the current CK (against the vectors of
// contracts/vectors/credential.json), the strict bodies of contracts/credentials/, opening with the author and epoch
// rules, put with expected_version and the stored ID, delete and the list in the `credentials` slice.
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { parseJson } from '../../../src/core/encoding/json';
import { openCredential } from '../../../src/core/crypto/credential';
import { Secret } from '../../../src/core/crypto/secret';
import {
  currentKey,
  deleteCredential,
  fetchRecord,
  isCredentialKind,
  listCredentials,
  loadCredentials,
  openRecord,
  ownKeys,
  putCredential,
  sealRecord,
  summarize,
  validBody,
} from '../../../src/core/credentials/records';
import { HubError } from '../../../src/core/net/signedFetch';
import { RequestError } from '../../../src/core/requests/client';
import { createStore } from '../../../src/core/store';
import {
  dismissCredentialRotation,
  MAX_NOTICES,
  noteCredentialRotation,
  setCredentials,
} from '../../../src/core/store/slices/credentials';
import { actor as vectorActor } from '../crypto/helpers';
import { VECTOR_FILES } from '../crypto/vector-suite';
import { actor, TS } from '../log/world';
import { API_KEY, credWorld, GITHUB, LOGIN_TOKEN, MAILBOX, seed, selfWrap, storedRecord, SUBSCRIPTION } from './world';

const codeOf = async (run: () => Promise<unknown>): Promise<string> => {
  try {
    await run();
  } catch (error) {
    return (error as { code: string }).code;
  }
  return 'none';
};

describe('sealRecord', () => {
  it('builds the record of the vector byte for byte', async () => {
    const data = VECTOR_FILES.credential;
    const record = data.record;
    const alice = await vectorActor('alice');
    const salt = b64uDecode(record.header.salt);
    const nonce = b64uDecode(record.header.nonce);
    const built = await sealRecord({
      id: record.header.id,
      owner: record.header.owner,
      kind: 'github',
      version: record.header.version,
      epoch: record.header.epoch,
      author: record.header.author,
      ts: record.header.ts,
      key: Secret.of(b64uDecode(data.cred_key)),
      body: parseJson(new TextEncoder().encode(data.body_json)),
      signer: alice.sig.privateKey,
      saltNonce: [salt, nonce],
    });
    expect(built).toEqual(record);
  });

  it('refuses a body the kind does not allow', async () => {
    const alice = await vectorActor('alice');
    const code = await codeOf(() =>
      sealRecord({
        id: '0190f000-0000-7000-8000-000000000001',
        owner: '0190f000-0000-7000-8000-000000000002',
        kind: 'anthropic_api_key',
        version: 1,
        epoch: 1,
        author: '0190f000-0000-7000-8000-000000000002',
        ts: TS,
        key: Secret.random(),
        body: { key: 'has space' },
        signer: alice.sig.privateKey,
      }),
    );
    expect(code).toBe('params_invalid');
  });
});

describe('bodies', () => {
  it('accepts exactly the contract bodies', () => {
    expect(validBody('anthropic_api_key', API_KEY)).toBe(true);
    expect(validBody('anthropic_api_key', { key: '' })).toBe(false);
    expect(validBody('anthropic_api_key', { key: 'a', extra: 1 })).toBe(false);
    expect(validBody('anthropic_api_key', 'x')).toBe(false);
    expect(validBody('mailbox', MAILBOX)).toBe(true);
    for (const broken of [
      { imap_host: 'bad host' },
      { imap_port: 0 },
      { imap_port: 70000 },
      { imap_port: 1.5 },
      { imap_user: '' },
      { imap_user: 7 },
      { imap_password: '' },
      { imap_password: 'x'.repeat(1025) },
      { imap_password: '😀'.repeat(1025) },
      { imap_password: 3 },
      { email: 'not-an-email' },
      { imap_host: 3 },
    ]) {
      expect(validBody('mailbox', { ...MAILBOX, ...broken })).toBe(false);
    }
    expect(validBody('mailbox', { ...MAILBOX, extra: 1 })).toBe(false);
    // the contract counts code points, not UTF-16 units
    expect(validBody('mailbox', { ...MAILBOX, imap_password: '😀'.repeat(1000) })).toBe(true);
    expect(validBody('claude_subscription', SUBSCRIPTION)).toBe(true);
    expect(validBody('claude_subscription', { ...SUBSCRIPTION, email: null })).toBe(true);
    expect(validBody('claude_subscription', { ...SUBSCRIPTION, email: 'x' })).toBe(false);
    expect(validBody('claude_subscription', { ...SUBSCRIPTION, kind: 'oauth' })).toBe(false);
    expect(validBody('claude_subscription', { ...SUBSCRIPTION, token: 7 })).toBe(false);
    // the login token of the local Claude Code login (ADR-056 section 7), with its expiry
    expect(validBody('claude_subscription', LOGIN_TOKEN)).toBe(true);
    expect(validBody('claude_subscription', { ...LOGIN_TOKEN, expires_at: '2026-11-23' })).toBe(false);
    expect(validBody('claude_subscription', { ...LOGIN_TOKEN, expires_at: 7 })).toBe(false);
    expect(validBody('claude_subscription', { ...LOGIN_TOKEN, extra: 1 })).toBe(false);
    expect(validBody('claude_subscription', { ...SUBSCRIPTION, kind: 'login_token' })).toBe(true);
    expect(validBody('github', GITHUB)).toBe(true);
    expect(validBody('github', { ...GITHUB, created_at: 'yesterday' })).toBe(false);
    expect(isCredentialKind('mailbox')).toBe(true);
    expect(isCredentialKind('ssh')).toBe(false);
  });

  it('summarizes without any secret', () => {
    expect(summarize('github', GITHUB)).toEqual({ kind: 'github', login: 'octo-cat' });
    expect(summarize('anthropic_api_key', API_KEY)).toEqual({ kind: 'anthropic_api_key' });
    expect(summarize('claude_subscription', SUBSCRIPTION)).toEqual({
      kind: 'claude_subscription',
      email: 'claude@example.com',
      origin: 'setup_token',
      expiresAt: null,
    });
    expect(summarize('claude_subscription', LOGIN_TOKEN)).toEqual({
      kind: 'claude_subscription',
      email: null,
      origin: 'login_token',
      expiresAt: '2026-11-23T20:00:00.000Z',
    });
    expect(summarize('mailbox', MAILBOX)).toEqual({
      kind: 'mailbox',
      email: 'claude@example.com',
      host: 'imap.example.com',
    });
    expect(JSON.stringify(summarize('mailbox', MAILBOX))).not.toContain('password');
  });
});

describe('ownKeys', () => {
  it('opens the own self-wraps newest first and skips foreign, broken and duplicate ones', async () => {
    const t = await credWorld();
    const two = Secret.of(new Uint8Array(32).fill(9));
    const other = await actor('other-user');
    const good = await selfWrap(t, 2, two, '2026-11-21T12:30:00.000Z');
    t.hub.wraps.push(
      good,
      { ...good },
      { ...good, epoch: 3, wrap: good.wrap }, // signature over epoch 2: refused
      { ...good, wrapped_by: other.id },
      { ...good, recipient_id: other.id },
      { ...good, scope: 'wg' },
      { ...good, scope_id: other.id },
      { ...good, epoch: 0 },
      { ...good, epoch: 'x' },
      { ...good, wrap: 7 },
      'junk' as never,
      { ...(await selfWrap(t, 4, two, 'not a time')), sig: good.sig },
    );
    const keys = await ownKeys(t.context, t.me.id, t.me.identity);
    expect(keys.map((key) => [key.epoch, key.createdAt])).toEqual([
      [2, Date.parse('2026-11-21T12:30:00.000Z')],
      [1, Date.parse('2026-11-21T11:00:00.000Z')],
    ]);
    const plain = await selfWrap(t, 5, two, null);
    t.client.on('GET /v1/wraps', () => ({ wraps: [plain] }));
    expect((await currentKey(t.context, t.me.id, t.me.identity)).createdAt).toBeNull();
    t.client.on('GET /v1/wraps', () => ({}));
    expect(await codeOf(() => currentKey(t.context, t.me.id, t.me.identity))).toBe('epoch_stale');
  });
});

describe('openRecord', () => {
  it('opens records of the user and of entitled devices and refuses everything else', async () => {
    const t = await credWorld();
    const keys = await ownKeys(t.context, t.me.id, t.me.identity);
    const own = await storedRecord(t, 'anthropic_api_key', API_KEY);
    expect(await openRecord(t.context, 'anthropic_api_key', own, keys)).toMatchObject({
      body: API_KEY,
      summary: { kind: 'anthropic_api_key' },
      header: { author: t.me.id, version: 1 },
    });
    const byDevice = await storedRecord(t, 'claude_subscription', SUBSCRIPTION, { author: t.device });
    expect((await openRecord(t.context, 'claude_subscription', byDevice, keys)).summary).toEqual({
      kind: 'claude_subscription',
      email: 'claude@example.com',
      origin: 'setup_token',
      expiresAt: null,
    });
    const stranger = await actor('stranger');
    const foreign = await storedRecord(t, 'anthropic_api_key', API_KEY, { author: stranger });
    expect(await codeOf(() => openRecord(t.context, 'anthropic_api_key', foreign, keys))).toBe('author_not_writer');
    expect(await codeOf(() => openRecord(t.context, 'mailbox', own, keys))).toBe('header_invalid');
    expect(await codeOf(() => openRecord(t.context, 'anthropic_api_key', 'junk', keys))).toBe('header_invalid');
    expect(await codeOf(() => openRecord(t.context, 'anthropic_api_key', { header: 'x' }, keys))).toBe(
      'header_invalid',
    );
    const later = await storedRecord(t, 'anthropic_api_key', API_KEY, { epoch: 2 });
    expect(await codeOf(() => openRecord(t.context, 'anthropic_api_key', later, keys))).toBe('epoch_stale');
    const odd = await storedRecord(t, 'anthropic_api_key', { key: 'a', note: 'b' });
    expect(await codeOf(() => openRecord(t.context, 'anthropic_api_key', odd, keys))).toBe('params_invalid');
    const tampered = { ...own, ct: own.ct.slice(0, -2) + (own.ct.endsWith('AA') ? 'BA' : 'AA') };
    expect(await codeOf(() => openRecord(t.context, 'anthropic_api_key', tampered, keys))).toBe('signature_invalid');
    const viaAuthors = await openRecord(t.context, 'anthropic_api_key', foreign, keys, () =>
      stranger.identity.sigPublic(),
    );
    expect(viaAuthors.body).toEqual(API_KEY);
  });

  it('needs a signed-in user', async () => {
    const t = await credWorld();
    const record = await storedRecord(t, 'anthropic_api_key', API_KEY);
    const bare = { ...t.context, store: createStore() };
    expect(await codeOf(() => openRecord(bare, 'anthropic_api_key', record, []))).toBe('unauthorized');
    expect(await codeOf(() => loadCredentials(bare))).toBe('unauthorized');
    expect(await codeOf(() => putCredential(bare, 'anthropic_api_key', API_KEY))).toBe('unauthorized');
  });
});

describe('list and fetch', () => {
  it('lists sorted by kind and refuses malformed answers', async () => {
    const t = await credWorld();
    await seed(t, 'mailbox', MAILBOX);
    await seed(t, 'github', GITHUB, { author: t.device });
    expect((await listCredentials(t.context)).map((item) => item.kind)).toEqual(['github', 'mailbox']);
    for (const answer of [
      null,
      {},
      { credentials: [{ kind: 'ssh', version: 1, epoch: 1, updated_at: TS }] },
      { credentials: [{ kind: 'github', version: 0, epoch: 1, updated_at: TS }] },
      { credentials: [{ kind: 'github', version: 1, epoch: 0, updated_at: TS }] },
      { credentials: [{ kind: 'github', version: 1, epoch: 1, updated_at: 3 }] },
      { credentials: ['x'] },
    ]) {
      t.client.on('GET /v1/me/credentials', () => answer);
      expect(await codeOf(() => listCredentials(t.context))).toBe('unavailable');
    }
  });

  it('fetches a record, null when there is none, and passes other failures on', async () => {
    const t = await credWorld();
    expect(await fetchRecord(t.context, 'mailbox')).toBeNull();
    t.client.on('GET /v1/me/credentials/mailbox', () => 'junk');
    expect(await fetchRecord(t.context, 'mailbox')).toBeNull();
    t.client.on('GET /v1/me/credentials/mailbox', () => ({}));
    expect(await fetchRecord(t.context, 'mailbox')).toBeNull();
    t.client.on('GET /v1/me/credentials/mailbox', () => {
      throw new HubError('unavailable', 503);
    });
    expect(await codeOf(() => fetchRecord(t.context, 'mailbox'))).toBe('unavailable');
  });
});

describe('loadCredentials', () => {
  it('opens every record into the slice, keeps unverifiable ones with their code', async () => {
    const t = await credWorld();
    expect(await loadCredentials(t.context)).toEqual([]);
    expect(t.client.of('GET /v1/wraps')).toHaveLength(0);
    await seed(t, 'github', GITHUB, { author: t.device });
    await seed(t, 'anthropic_api_key', API_KEY);
    const stranger = await actor('stranger');
    await seed(t, 'mailbox', MAILBOX, { author: stranger });
    t.hub.records.set('claude_subscription', { ...t.hub.records.get('mailbox')!, record: 'junk' as never });
    const entries = await loadCredentials(t.context);
    expect(entries).toEqual([
      expect.objectContaining({ kind: 'github', status: 'verified', summary: { kind: 'github', login: 'octo-cat' } }),
      expect.objectContaining({ kind: 'anthropic_api_key', status: 'verified', code: null }),
      expect.objectContaining({ kind: 'claude_subscription', status: 'unverifiable', code: 'header_invalid' }),
      expect.objectContaining({ kind: 'mailbox', status: 'unverifiable', code: 'author_not_writer', summary: null }),
    ]);
    expect(t.store.getState().credentials.entries).toEqual(entries);
    expect(JSON.stringify(t.store.getState())).not.toContain(API_KEY.key);
  });

  it('skips a record deleted in between, reports a non-protocol failure as unavailable and passes hub errors on', async () => {
    const t = await credWorld();
    await seed(t, 'anthropic_api_key', API_KEY);
    t.client.on('GET /v1/me/credentials/anthropic_api_key', () => {
      throw new HubError('not_found', 404);
    });
    expect(await loadCredentials(t.context)).toEqual([]);
    t.client.on('GET /v1/me/credentials/anthropic_api_key', () => ({
      record: { header: { owner: t.me.id, kind: 'anthropic_api_key', author: t.me.id }, ct: 1, sig: 2 },
    }));
    const [entry] = await loadCredentials(t.context);
    expect(entry).toMatchObject({ status: 'unverifiable' });
    t.client.on('GET /v1/wraps', () => {
      throw new HubError('rate_limited', 429);
    });
    expect(await codeOf(() => loadCredentials(t.context))).toBe('rate_limited');
    t.client.on('GET /v1/wraps', () => ({ wraps: t.hub.wraps }));
    const original = t.context.store.getState().me.identity!;
    const broken = Object.create(original) as typeof original;
    Object.defineProperty(broken, 'sigPublic', {
      value: () => {
        throw new TypeError('boom');
      },
    });
    t.context.store.update('me', (me) => ({ ...me, identity: broken }));
    t.client.on('GET /v1/me/credentials/anthropic_api_key', () => ({
      record: t.hub.records.get('anthropic_api_key')!.record,
    }));
    const [failed] = await loadCredentials(t.context);
    expect(failed).toMatchObject({ status: 'unverifiable', code: 'unavailable' });
  });

  it('writes nothing after a sign-out', async () => {
    const t = await credWorld();
    await seed(t, 'anthropic_api_key', API_KEY);
    t.client.on('GET /v1/me/credentials', () => {
      t.store.reset();
      return t.hub.list();
    });
    const identity = t.me.identity;
    t.client.on('GET /v1/wraps', () => ({ wraps: t.hub.wraps }));
    // the reset clears the user: the open step needs it, so the record is reported unverifiable but not stored
    await loadCredentials(t.context).catch(() => undefined);
    expect(t.store.getState().credentials.entries).toBeNull();
    expect(identity).toBeTruthy();
  });
});

describe('putCredential', () => {
  it('creates, then updates with the stored ID and version, readable with CK', async () => {
    const t = await credWorld();
    expect(await putCredential(t.context, 'anthropic_api_key', API_KEY)).toBe(1);
    const first = t.hub.records.get('anthropic_api_key')!;
    expect(first.record.header).toMatchObject({ owner: t.me.id, author: t.me.id, kind: 'anthropic_api_key', epoch: 1 });
    expect(t.client.of('PUT /v1/me/credentials/anthropic_api_key')[0]!.body).toMatchObject({ expected_version: 0 });
    expect(JSON.stringify(t.client.calls)).not.toContain(API_KEY.key);
    const opened = await openCredential(t.credKey, first.record, t.me.identity.sigPublic());
    expect(parseJson(opened)).toEqual(API_KEY);
    expect(await putCredential(t.context, 'anthropic_api_key', { key: 'sk-ant-second' })).toBe(2);
    const second = t.hub.records.get('anthropic_api_key')!;
    expect(second.record.header.id).toBe(first.record.header.id);
    expect(second.version).toBe(2);
  });

  it('retries once after a version conflict or a stale epoch', async () => {
    const t = await credWorld();
    let conflicts = 1;
    t.client.on('PUT /v1/me/credentials/mailbox', (body) => {
      if (conflicts-- > 0) throw new HubError('version_conflict', 409);
      return t.hub.put('mailbox', body as never);
    });
    expect(await putCredential(t.context, 'mailbox', MAILBOX)).toBe(1);
    expect(t.client.of('PUT /v1/me/credentials/mailbox')).toHaveLength(2);
    t.client.on('PUT /v1/me/credentials/mailbox', () => {
      throw new HubError('epoch_stale', 400);
    });
    expect(await codeOf(() => putCredential(t.context, 'mailbox', MAILBOX))).toBe('epoch_stale');
    t.client.on('PUT /v1/me/credentials/mailbox', () => {
      throw new HubError('forbidden', 403);
    });
    expect(await codeOf(() => putCredential(t.context, 'mailbox', MAILBOX))).toBe('forbidden');
    t.client.on('PUT /v1/me/credentials/mailbox', () => {
      throw new TypeError('network');
    });
    expect(await codeOf(() => putCredential(t.context, 'mailbox', MAILBOX))).toBeUndefined();
  });

  it('refuses invalid bodies and unexpected answers, and takes a fresh ID when the stored one is unreadable', async () => {
    const t = await credWorld();
    expect(await codeOf(() => putCredential(t.context, 'mailbox', { ...MAILBOX, imap_port: 0 }))).toBe(
      'params_invalid',
    );
    t.client.on('PUT /v1/me/credentials/mailbox', () => ({ version: 9 }));
    expect(await codeOf(() => putCredential(t.context, 'mailbox', MAILBOX))).toBe('unavailable');
    t.client.on('PUT /v1/me/credentials/mailbox', () => null);
    expect(await codeOf(() => putCredential(t.context, 'mailbox', MAILBOX))).toBe('unavailable');
    t.hub.records.set('mailbox', { record: { header: 'x' } as never, version: 3, epoch: 1, updated_at: TS });
    t.client.on('PUT /v1/me/credentials/mailbox', (body) => ({
      version: (body as { record: { header: { version: number } } }).record.header.version,
    }));
    expect(await putCredential(t.context, 'mailbox', MAILBOX)).toBe(4);
    const sent = t.client.of('PUT /v1/me/credentials/mailbox').at(-1)!.body as { record: { header: { id: string } } };
    expect(sent.record.header.id).toMatch(/^[0-9a-f-]{36}$/);
    t.hub.records.set('mailbox', { record: { header: { id: 7 } } as never, version: 3, epoch: 1, updated_at: TS });
    expect(await putCredential(t.context, 'mailbox', MAILBOX)).toBe(4);
  });
});

describe('deleteCredential', () => {
  it('deletes, treats a missing record as done and passes other failures on', async () => {
    const t = await credWorld();
    await seed(t, 'mailbox', MAILBOX);
    await deleteCredential(t.context, 'mailbox');
    expect(t.hub.records.has('mailbox')).toBe(false);
    await deleteCredential(t.context, 'mailbox');
    t.client.on('DELETE /v1/me/credentials/mailbox', () => {
      throw new HubError('forbidden', 403);
    });
    expect(await codeOf(() => deleteCredential(t.context, 'mailbox'))).toBe('forbidden');
    t.client.on('DELETE /v1/me/credentials/mailbox', () => {
      throw new TypeError('network');
    });
    expect(await codeOf(() => deleteCredential(t.context, 'mailbox'))).toBeUndefined();
  });
});

describe('credentials slice', () => {
  it('keeps entries and at most three rotation notices', () => {
    const store = createStore();
    setCredentials(store, []);
    expect(store.getState().credentials.entries).toEqual([]);
    for (let index = 0; index < MAX_NOTICES + 1; index += 1) {
      noteCredentialRotation(store, {
        result: { status: 'blocked', kinds: ['mailbox'] },
        at: index,
      });
    }
    const notices = store.getState().credentials.notices;
    expect(notices.map((notice) => notice.at)).toEqual([1, 2, 3]);
    const before = store.getState().credentials;
    dismissCredentialRotation(store, 999);
    expect(store.getState().credentials).toBe(before);
    dismissCredentialRotation(store, notices[0]!.id);
    expect(store.getState().credentials.notices.map((notice) => notice.at)).toEqual([2, 3]);
  });

  it('is a RequestError for client refusals', () => {
    expect(new RequestError('params_invalid').code).toBe('params_invalid');
  });
});

describe('mailboxBody', () => {
  it('builds the body of the form or null', async () => {
    const { mailboxBody } = await import('../../../src/core/credentials/records');
    const form = { ...MAILBOX, imap_port: ' 993 ', imap_host: ' imap.example.com ' };
    expect(mailboxBody(form)).toEqual(MAILBOX);
    expect(mailboxBody({ ...form, imap_port: 'x' })).toBeNull();
    expect(mailboxBody({ ...form, email: '' })).toBeNull();
  });
});
