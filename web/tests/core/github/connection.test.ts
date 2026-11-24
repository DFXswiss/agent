// The GitHub connection (06-web.md section 8.1): records verified and opened in the browser (author user or an
// entitled device, the user's CK of the record's epoch, only the login leaves), disconnect, the device choice with the
// automatic assignment for a fresh org, and the connect progress from the opened responses.
import { describe, expect, it } from 'vitest';
import { bodyJson } from '../../../src/core/encoding/json';
import { newHeader, sealCredential } from '../../../src/core/crypto/credential';
import { Secret } from '../../../src/core/crypto/secret';
import {
  authorKey,
  connectDevices,
  connectProgress,
  credentialKey,
  githubWays,
  offersSignIn,
  defaultWorkgroup,
  disconnectGithub,
  loadGithub,
  openGithubRecord,
  startConnect,
  tokenInput,
} from '../../../src/core/github/connection';
import { openRequest } from '../../../src/core/crypto/request';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { HubError } from '../../../src/core/net/signedFetch';
import type { RequestView } from '../../../src/core/requests/client';
import { setProfile } from '../../../src/core/store/slices/me';
import { setLog } from '../../../src/core/store/slices/orgs';
import { setActiveOrg } from '../../../src/core/store/slices/session';
import { actor, type Actor, T0 } from '../log/world';
import { candidate, taskWorld, type TaskWorld } from '../tasks/world';

const BODY = {
  token: 'gho_not_real',
  login: 'octo-cat',
  user_id: 42,
  scopes: [],
  created_at: '2026-11-21T12:00:00.000Z',
};

async function world(): Promise<TaskWorld & { device: Actor }> {
  const t = await taskWorld();
  const device = await actor('gh-device');
  await t.world.admitDevice(t.me, device, [t.wg]);
  setLog(t.store, t.org, t.world.log!);
  t.client.on('GET /v1/wraps', () => ({ wraps: [t.credWrap] }));
  return { ...t, device };
}

async function record(
  t: TaskWorld,
  author: Actor,
  options: { body?: unknown; owner?: string; kind?: string; epoch?: number; key?: Secret } = {},
) {
  const header = newHeader({
    id: '0190f000-0000-7000-8000-0000000000c1',
    owner: options.owner ?? t.me.id,
    kind: options.kind ?? 'github',
    version: 2,
    epoch: options.epoch ?? 1,
    author: author.id,
    ts: '2026-11-21T12:00:00.000Z',
  });
  return sealCredential(
    options.key ?? t.credKey,
    header,
    bodyJson(options.body ?? BODY),
    author.identity.sig.privateKey,
  );
}

function serve(t: TaskWorld, found: unknown) {
  t.client.on('GET /v1/me/credentials', () => ({
    credentials: [{ kind: 'github', version: 2, epoch: 1, updated_at: '2026-11-21T12:00:00.000Z' }],
  }));
  t.client.on('GET /v1/me/credentials/github', () => ({ record: found }));
}

describe('loadGithub', () => {
  it('opens a record written by the entitled device and keeps only the login', async () => {
    const t = await world();
    serve(t, await record(t, t.device));
    const connection = await loadGithub(t.context);
    expect(connection).toEqual({
      status: 'connected',
      login: 'octo-cat',
      scopes: [],
      source: 'device_flow',
      createdAt: BODY.created_at,
      version: 2,
      // an OAuth App token of the device flow (KOR-65): the devices revoke it on disconnect
      revocable: true,
    });
    expect(t.store.getState().github).toEqual(connection);
    expect(JSON.stringify(t.store.getState().github)).not.toContain('gho_');
  });

  it('reads the source and the scopes of a stored token', async () => {
    const t = await world();
    serve(t, await record(t, t.device, { body: { ...BODY, scopes: ['repo', 'read:org'], source: 'token' } }));
    expect(await loadGithub(t.context)).toMatchObject({
      source: 'token',
      scopes: ['repo', 'read:org'],
      revocable: false,
    });
    serve(t, await record(t, t.device, { body: { ...BODY, token: 'ghp_not_real' } }));
    expect(await loadGithub(t.context)).toMatchObject({ source: 'device_flow', revocable: false });
    serve(t, await record(t, t.device, { body: { ...BODY, token: 'ghu_not_real' } }));
    expect(await loadGithub(t.context)).toMatchObject({ revocable: true });
    serve(t, await record(t, t.device, { body: { ...BODY, source: 'device_flow' } }));
    expect(await loadGithub(t.context)).toMatchObject({ source: 'device_flow', scopes: [] });
    serve(t, await record(t, t.device, { body: { ...BODY, source: 'paste' } }));
    expect(await loadGithub(t.context)).toEqual({ status: 'unverifiable', code: 'params_invalid' });
  });

  it('opens a record written by the user', async () => {
    const t = await world();
    serve(t, await record(t, t.me));
    expect(await loadGithub(t.context)).toMatchObject({ status: 'connected' });
  });

  it('is disconnected without a github record, or when the record vanished', async () => {
    const t = await world();
    t.client.on('GET /v1/me/credentials', () => ({ credentials: [{ kind: 'mailbox' }] }));
    expect(await loadGithub(t.context)).toEqual({ status: 'disconnected' });
    t.client.on('GET /v1/me/credentials', () => null);
    expect(await loadGithub(t.context)).toEqual({ status: 'disconnected' });
    serve(t, null);
    t.client.on('GET /v1/me/credentials/github', () => {
      throw new HubError('not_found', 404);
    });
    expect(await loadGithub(t.context)).toEqual({ status: 'disconnected' });
  });

  it('passes other hub errors on', async () => {
    const t = await world();
    serve(t, null);
    t.client.on('GET /v1/me/credentials/github', () => {
      throw new HubError('unavailable', 503);
    });
    await expect(loadGithub(t.context)).rejects.toMatchObject({ code: 'unavailable' });
  });

  it('reports forged and foreign records as unverifiable', async () => {
    const t = await world();
    const stranger = await actor('stranger');
    const cases: [unknown, string][] = [
      [await record(t, stranger), 'author_not_writer'],
      [await record(t, t.device, { owner: stranger.id }), 'header_invalid'],
      [await record(t, t.device, { kind: 'mailbox' }), 'header_invalid'],
      [await record(t, t.device, { epoch: 2 }), 'epoch_stale'],
      [await record(t, t.device, { key: Secret.of(new Uint8Array(32).fill(9)) }), 'decrypt_failed'],
      [await record(t, t.device, { body: { ...BODY, login: 'bad login' } }), 'params_invalid'],
      [await record(t, t.device, { body: { login: 'octo-cat', created_at: BODY.created_at } }), 'params_invalid'],
      [await record(t, t.device, { body: { ...BODY, created_at: 'yesterday' } }), 'header_invalid'],
      [await record(t, t.device, { body: { ...BODY, extra: 1 } }), 'params_invalid'],
      [await record(t, t.device, { body: { ...BODY, token: 'with space' } }), 'params_invalid'],
      [await record(t, t.device, { body: { ...BODY, user_id: 0 } }), 'params_invalid'],
      [await record(t, t.device, { body: { ...BODY, user_id: 'x' } }), 'params_invalid'],
      [await record(t, t.device, { body: { ...BODY, scopes: 'repo' } }), 'params_invalid'],
      [await record(t, t.device, { body: { ...BODY, scopes: ['Bad Scope'] } }), 'params_invalid'],
      [
        await record(t, t.device, { body: { ...BODY, scopes: Array.from({ length: 65 }, () => 'repo') } }),
        'params_invalid',
      ],
      [await record(t, t.device, { body: { ...BODY, token: 5 } }), 'params_invalid'],
      [await record(t, t.device, { body: { ...BODY, created_at: 5 } }), 'params_invalid'],
      [await record(t, t.device, { body: { ...BODY, scopes: ['repo', 'read:user'] } }), 'connected'],
      ['junk', 'header_invalid'],
    ];
    serve(t, null);
    t.client.on('GET /v1/me/credentials/github', () => 'junk');
    expect(await loadGithub(t.context)).toEqual({ status: 'unverifiable', code: 'header_invalid' });
    for (const [found, code] of cases) {
      serve(t, found);
      const loaded = await loadGithub(t.context);
      expect(loaded.status === 'unverifiable' ? loaded.code : loaded.status).toBe(code);
    }
    const signed = (await record(t, t.device)) as unknown as { sig: string };
    serve(t, { ...signed, sig: (await record(t, t.me)).sig });
    expect(await loadGithub(t.context)).toEqual({ status: 'unverifiable', code: 'signature_invalid' });
    serve(t, await record(t, t.device));
    t.client.on('GET /v1/me/credentials/github', () => {
      throw new TypeError('network');
    });
    expect(await loadGithub(t.context)).toEqual({ status: 'unverifiable', code: 'unavailable' });
  });

  it('writes nothing after a sign-out and refuses anonymous opening', async () => {
    const t = await world();
    serve(t, await record(t, t.device));
    const pending = loadGithub(t.context);
    t.store.reset();
    await pending;
    expect(t.store.getState().github).toEqual({ status: 'unknown' });
    await expect(openGithubRecord(t.context, null)).rejects.toMatchObject({ code: 'unauthorized' });
  });
});

describe('credentialKey and authorKey', () => {
  it('opens only the own self-wrap of the epoch', async () => {
    const t = await world();
    const own = t.credWrap;
    const variants = [
      { ...own, wrapped_by: t.device.id },
      { ...own, recipient_id: t.device.id },
      { ...own, scope: 'wg' },
      { ...own, scope_id: t.device.id },
      { ...own, epoch: 2 },
      { ...own, wrap: 1 },
      { ...own, sig: 'AAAA' },
    ];
    t.client.on('GET /v1/wraps', () => ({ wraps: [...variants, 'junk'] }));
    expect(await credentialKey(t.context, t.me.id, t.me.identity, 1)).toBeNull();
    t.client.on('GET /v1/wraps', () => ({ wraps: [...variants, own] }));
    const key = await credentialKey(t.context, t.me.id, t.me.identity, 1);
    expect(key!.reveal((raw) => [...raw])).toEqual(t.credKey.reveal((raw) => [...raw]));
    t.client.on('GET /v1/wraps', () => null);
    expect(await credentialKey(t.context, t.me.id, t.me.identity, 1)).toBeNull();
  });

  it('knows the user and entitled devices only', async () => {
    const t = await world();
    expect(authorKey(t.store, t.me.id, t.me.identity, t.me.id)).toEqual(t.me.identity.sigPublic());
    expect(authorKey(t.store, t.me.id, t.me.identity, t.device.id)).toEqual(t.device.identity.sigPublic());
    expect(authorKey(t.store, t.me.id, t.me.identity, 7)).toBeNull();
    expect(authorKey(t.store, t.me.id, t.me.identity, t.org)).toBeNull();
  });
});

describe('disconnectGithub', () => {
  it('deletes the record; a missing one counts as done; other errors pass on', async () => {
    const t = await world();
    t.client.on('DELETE /v1/me/credentials/github', () => ({}));
    await disconnectGithub(t.context);
    expect(t.store.getState().github).toEqual({ status: 'disconnected' });
    t.client.routes.delete('DELETE /v1/me/credentials/github');
    await disconnectGithub(t.context);
    t.client.on('DELETE /v1/me/credentials/github', () => {
      throw new HubError('unavailable', 503);
    });
    await expect(disconnectGithub(t.context)).rejects.toMatchObject({ code: 'unavailable' });
    // an answer after a sign-out writes nothing
    t.client.on('DELETE /v1/me/credentials/github', () => {
      t.store.reset();
      return {};
    });
    await disconnectGithub(t.context);
    expect(t.store.getState().github).toEqual({ status: 'unknown' });
  });
});

const meProfile = (t: TaskWorld) => ({ orgs: [{ org_id: t.org, role: 'admin', private: true, default_wg: t.wg }] });

describe('connectDevices', () => {
  it('lists verified devices, online and default-workgroup devices first, and preselects the first online one', async () => {
    const t = await world();
    setProfile(t.store, meProfile(t));
    const other = await actor('gh-other');
    await t.world.admitDevice(t.me, other, [t.wg]);
    setLog(t.store, t.org, t.world.log!);
    const item = (device: string, online: boolean, wgs: string[], org = t.org) => ({
      device_id: device,
      kind: 'org',
      org_id: org,
      wg_ids: wgs,
      online,
      last_seen_at: null,
    });
    t.client.on('GET /v1/me/devices', () => ({
      devices: [
        item(other.id, false, [t.wg]),
        item(t.device.id, true, ['0190f000-0000-7000-8000-00000000ffff']),
        item(t.device.id, false, [t.wg], '0190f000-0000-7000-8000-00000000eeee'),
        item(t.device.id, false, [t.wg]),
        item('0190f000-0000-7000-8000-0000000000aa', true, [t.wg]),
      ],
    }));
    const found = await connectDevices(t.context);
    expect(found.devices).toEqual([
      { device: t.device.id, kind: 'org', online: true, defaultWg: true, deviceFlow: true, oauthApp: false },
      { device: other.id, kind: 'org', online: false, defaultWg: true, deviceFlow: true, oauthApp: false },
    ]);
    expect(found.preselected).toBe(t.device.id);
  });

  it('orders equal devices by ID and has no preselection without an online device', async () => {
    const t = await world();
    const other = await actor('gh-other');
    await t.world.admitDevice(t.me, other, [t.wg]);
    setLog(t.store, t.org, t.world.log!);
    const item = (device: string) => ({
      device_id: device,
      kind: 'org',
      org_id: t.org,
      wg_ids: [t.wg],
      online: false,
      last_seen_at: null,
    });
    t.client.on('GET /v1/me/devices', () => ({ devices: [item(t.device.id), item(other.id)] }));
    const found = await connectDevices(t.context);
    expect(found.devices.map((d) => d.device)).toEqual([t.device.id, other.id].sort());
    expect(found.preselected).toBeNull();
  });

  it('assigns a platform device first when the user has none', async () => {
    const t = await taskWorld();
    setProfile(t.store, meProfile(t));
    setActiveOrg(t.store, t.org);
    const lists = [{ devices: [] }];
    t.client.on('GET /v1/me/devices', () => lists.shift() ?? { devices: [] });
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [] }));
    t.client.on(`GET /v1/workgroups/${t.wg}/device-candidates`, () => ({ candidates: [candidate(t, 0)] }));
    t.client.on(`POST /v1/orgs/${t.org}/log`, () => {
      throw new HubError('forbidden', 403);
    });
    await expect(connectDevices(t.context)).rejects.toMatchObject({ code: 'forbidden' });
    expect(t.client.of(`POST /v1/orgs/${t.org}/log`)).toHaveLength(1);
  });

  it('lists again after the workgroup turned out to have a device', async () => {
    const t = await taskWorld();
    setProfile(t.store, meProfile(t));
    t.client.on('GET /v1/me/devices', () => ({ devices: [] }));
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{ device_id: 'd' }] }));
    expect(await connectDevices(t.context)).toEqual({ devices: [], preselected: null });
    expect(t.client.of('GET /v1/me/devices')).toHaveLength(2);
  });

  it('offers only the devices of the organization of a company account, without an assignment', async () => {
    const t = await world();
    setProfile(t.store, meProfile(t));
    const item = (org: string) => ({
      device_id: t.device.id,
      kind: 'org',
      org_id: org,
      wg_ids: [t.wg],
      online: true,
      last_seen_at: null,
    });
    t.client.on('GET /v1/me/devices', () => ({ devices: [item('0190f000-0000-7000-8000-00000000eeee')] }));
    expect(await connectDevices(t.context, t.org)).toEqual({ devices: [], preselected: null });
    expect(t.client.of('GET /v1/me/devices')).toHaveLength(1);
    t.client.on('GET /v1/me/devices', () => ({ devices: [item(t.org)] }));
    expect((await connectDevices(t.context, t.org)).preselected).toBe(t.device.id);
  });

  it('does not assign without a profile', async () => {
    const t = await taskWorld();
    t.client.on('GET /v1/me/devices', () => ({ devices: [] }));
    expect(await connectDevices(t.context)).toEqual({ devices: [], preselected: null });
  });
});

describe('defaultWorkgroup', () => {
  it('is the active org of the profile, else the first', async () => {
    const t = await taskWorld();
    expect(defaultWorkgroup(t.store)).toBeNull();
    setProfile(t.store, { orgs: [{ org_id: 'a', default_wg: 'wa' }, { org_id: 'b', default_wg: 'wb' }, 'junk'] });
    expect(defaultWorkgroup(t.store)).toEqual({ org: 'a', wg: 'wa' });
    setActiveOrg(t.store, 'b');
    expect(defaultWorkgroup(t.store)).toEqual({ org: 'b', wg: 'wb' });
    setProfile(t.store, { orgs: [{ org_id: 'b' }] });
    expect(defaultWorkgroup(t.store)).toBeNull();
  });
});

describe('startConnect and connectProgress', () => {
  it('posts a github.connect request to the device', async () => {
    const t = await world();
    t.client.on('POST /v1/device-requests', (body) => ({
      request_id: (body as { envelope: { header: { id: string } } }).envelope.header.id,
      state: 'pending',
    }));
    const prepared = await startConnect(t.context, t.device.id);
    expect(prepared.kind).toBe('github.connect');
    expect(prepared.envelope.header).toMatchObject({ device: t.device.id, kind: 'github.connect' });
  });

  it('seals a token for the device only', async () => {
    const t = await world();
    t.client.on('POST /v1/device-requests', (body) => ({
      request_id: (body as { envelope: { header: { id: string } } }).envelope.header.id,
      state: 'pending',
    }));
    const prepared = await startConnect(t.context, t.device.id, 'ghp_not_a_real_token');
    const opened = JSON.parse(
      fromUtf8(await openRequest(t.device.identity.kem, prepared.envelope, t.me.identity.sigPublic())),
    );
    expect(opened).toEqual({ token: 'ghp_not_a_real_token' });
    const signIn = await startConnect(t.context, t.device.id, undefined, 'oauth_app');
    const named = JSON.parse(
      fromUtf8(await openRequest(t.device.identity.kem, signIn.envelope, t.me.identity.sigPublic())),
    );
    expect(named).toEqual({ app: 'oauth_app' });
    expect(JSON.stringify(t.client.calls)).not.toContain('ghp_not_a_real_token');
    await expect(startConnect(t.context, t.device.id, 'with space')).rejects.toMatchObject({
      code: 'params_invalid',
    });
  });

  it('names the identity, the organization (also in plaintext) and the signing key (KOR-47)', async () => {
    const t = await world();
    t.client.on('POST /v1/device-requests', (body) => ({
      request_id: (body as { envelope: { header: { id: string } } }).envelope.header.id,
      state: 'pending',
    }));
    const id = '0190f000-0000-7000-8000-0000000000e1';
    const prepared = await startConnect(t.context, t.device.id, undefined, {
      orgId: t.org,
      identityId: id,
      signingKey: true,
    });
    const opened = JSON.parse(
      fromUtf8(await openRequest(t.device.identity.kem, prepared.envelope, t.me.identity.sigPublic())),
    );
    expect(opened).toEqual({ org_id: t.org, identity_id: id, signing_key: true });
    expect(prepared.orgId).toBe(t.org);
    expect(t.client.calls.at(-1)!.body).toEqual({ envelope: prepared.envelope, org_id: t.org });
    const plain = await startConnect(t.context, t.device.id, undefined, { signingKey: false });
    expect(
      JSON.parse(fromUtf8(await openRequest(t.device.identity.kem, plain.envelope, t.me.identity.sigPublic()))),
    ).toEqual({});
    expect(t.client.calls.at(-1)!.body).toEqual({ envelope: plain.envelope });
  });

  it('trims a pasted token and refuses what cannot be one', () => {
    expect(tokenInput('  ghp_abc\n')).toBe('ghp_abc');
    expect(tokenInput('')).toBeNull();
    expect(tokenInput('gh p')).toBeNull();
    expect(tokenInput('ä')).toBeNull();
    expect(tokenInput('x'.repeat(4097))).toBeNull();
  });

  const base: RequestView = { id: 'r', state: 'delivered', expiresAt: T0, responses: [] };
  const verified = (seq: number, body: unknown, final = false) =>
    ({ seq, status: 'verified', final, at: T0, body }) as RequestView['responses'][number];

  it('derives each step', () => {
    expect(connectProgress(base)).toEqual({ step: 'waiting' });
    const code = {
      step: 'device_code',
      user_code: 'ABCD-1234',
      verification_uri: 'https://github.com/login/device',
      expires_in: 900,
    };
    expect(connectProgress({ ...base, responses: [verified(1, code)] })).toEqual({
      step: 'device_code',
      userCode: 'ABCD-1234',
      verificationUri: 'https://github.com/login/device',
      expiresAt: T0 + 900_000,
    });
    expect(
      connectProgress({ ...base, responses: [verified(1, code), { seq: 2, status: 'unverifiable', code: 'x' }] }),
    ).toMatchObject({ step: 'device_code' });
    expect(connectProgress({ ...base, responses: [verified(2, { step: 'done', login: 'octo' }, true)] })).toEqual({
      step: 'done',
      login: 'octo',
      scopes: null,
      source: null,
      identityId: null,
      signing: null,
    });
    const token = { step: 'done', login: 'octo', scopes: ['repo'], source: 'token' };
    expect(connectProgress({ ...base, responses: [verified(1, token, true)] })).toEqual({
      step: 'done',
      login: 'octo',
      scopes: ['repo'],
      source: 'token',
      identityId: null,
      signing: null,
    });
    const named = { ...token, identity_id: '0190f000-0000-7000-8000-0000000000e1', signing: 'registered' };
    expect(connectProgress({ ...base, responses: [verified(1, named, true)] })).toMatchObject({
      identityId: named.identity_id,
      signing: 'registered',
    });
    expect(connectProgress({ ...base, responses: [verified(1, { step: 'failed', code: 'busy' }, true)] })).toEqual({
      step: 'failed',
      code: 'busy',
    });
    expect(connectProgress({ ...base, state: 'expired' })).toEqual({ step: 'failed', code: 'timeout' });
    expect(connectProgress({ ...base, state: 'failed' })).toEqual({ step: 'failed', code: 'request_failed' });
  });
});

describe('GitHub ways of the devices (KOR-65)', () => {
  const item = (device: string, features?: string[]) => ({
    device_id: device,
    kind: 'org',
    org_id: '0190f000-0000-7000-8000-00000000eeee',
    wg_ids: ['0190f000-0000-7000-8000-00000000ffff'],
    online: true,
    last_seen_at: null,
    ...(features === undefined ? {} : { features }),
  });

  it('knows which device runs a sign-in through GitHub', () => {
    expect(offersSignIn(item('a'))).toBe(true); // an older hub or runner: as before
    expect(offersSignIn(item('a', ['log.admit']))).toBe(true);
    expect(offersSignIn(item('a', ['github.ways']))).toBe(false);
    expect(offersSignIn(item('a', ['github.ways', 'github.device_flow']))).toBe(true);
    expect(offersSignIn(item('a', ['github.ways', 'github.operator']))).toBe(true);
  });

  it('offers the sign-in when any device runs it, and does not know without a device', async () => {
    const t = await world();
    const lists = [
      { devices: [] },
      { devices: [item('a', ['github.ways']), item('b', ['github.ways', 'github.device_flow'])] },
      { devices: [item('a', ['github.ways'])] },
    ];
    t.client.on('GET /v1/me/devices', () => lists.shift());
    expect(await githubWays(t.context)).toBeNull();
    expect(await githubWays(t.context)).toBe(true);
    expect(await githubWays(t.context)).toBe(false);
    t.client.on('GET /v1/me/devices', () => ({ devices: [{ ...item('a'), features: [1] }] }));
    await expect(githubWays(t.context)).rejects.toThrow();
  });

  it('marks the choices that run a sign-in, also when only one item of a device announces it', async () => {
    const t = await world();
    const device = (features: string[]) => ({ ...item(t.device.id, features), org_id: t.org, wg_ids: [t.wg] });
    t.client.on('GET /v1/me/devices', () => ({
      devices: [device(['github.ways', 'github.device_flow']), { ...device(['github.ways']), org_id: 'x' }],
    }));
    expect((await connectDevices(t.context)).devices.map((d) => d.deviceFlow)).toEqual([true]);
    t.client.on('GET /v1/me/devices', () => ({
      devices: [
        device(['github.ways', 'github.device_flow', 'github.oauth_app']),
        { ...device(['github.ways']), org_id: 'x' },
      ],
    }));
    expect((await connectDevices(t.context)).devices.map((d) => d.oauthApp)).toEqual([true]);
    t.client.on('GET /v1/me/devices', () => ({ devices: [device(['github.ways'])] }));
    expect((await connectDevices(t.context)).devices.map((d) => d.deviceFlow)).toEqual([false]);
  });
});
