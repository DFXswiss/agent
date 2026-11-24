// The device-request client (WEB-09): responses of the vectors (contracts/vectors/request.json) opened through
// `readRequest`, every negative response vector refused per response, forged hub answers, device keys only from the
// verified logs, and sealed requests that the addressed device opens.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../../helpers';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { Identity } from '../../../src/core/crypto/keys';
import { openRequest, sealResponse } from '../../../src/core/crypto/request';
import { CryptoError } from '../../../src/core/crypto/errors';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import { setIdentity, setSession } from '../../../src/core/store/slices/me';
import { blockOrg, ensureOrg, setLog, setPlatform } from '../../../src/core/store/slices/orgs';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import {
  deviceKeys,
  eligibleDevices,
  latestResponse,
  prepareRequest,
  type PreparedRequest,
  readRequest,
  RequestError,
  requestErrorCode,
  submitRequest,
} from '../../../src/core/requests/client';
import { actor, T0 } from '../log/world';
import { FakeClient, taskWorld, type TaskWorld } from '../tasks/world';

const VECTORS = JSON.parse(readFileSync(join(WEB_DIR, '..', 'contracts', 'vectors', 'request.json'), 'utf8'));
const EXPIRES = '2026-11-21T12:15:00.000Z';

/** A store whose user is the vector user (only the X25519 key is needed to open responses). */
async function vectorWorld(kemSk: string = VECTORS.user.kem_sk) {
  const header = VECTORS.requests[0].envelope.header;
  const store = createStore();
  setSession(store, header.user, { keyId: 'session.x', expiresAt: T0 + 3600_000 });
  setIdentity(store, await Identity.fromRaw(b64uDecode(kemSk), new Uint8Array(32).fill(1)));
  const client = new FakeClient();
  const prepared: PreparedRequest = {
    id: header.id,
    kind: header.kind,
    follows: '',
    device: { device: header.device, kem: VECTORS.device.kem_pk, sig: VECTORS.device.sig_pk },
    envelope: VECTORS.requests[0].envelope,
  };
  const serve = (responses: unknown[], extra: Record<string, unknown> = {}) =>
    client.on(`GET /v1/device-requests/${header.id}`, () => ({
      request_id: header.id,
      kind: header.kind,
      device_id: header.device,
      state: 'answered',
      created_at: '2026-11-21T12:00:00.000Z',
      expires_at: EXPIRES,
      responses,
      ...extra,
    }));
  return { store, client, prepared, serve, context: { store, client, now: () => T0 } };
}

const listed = (envelope: unknown, seq: number) => ({ seq, envelope, created_at: '2026-11-21T12:00:01.000Z' });

describe('readRequest against the vectors', () => {
  it('opens every response of the vectors in seq order', async () => {
    const v = await vectorWorld();
    v.serve(
      [...VECTORS.responses]
        .reverse()
        .map((item: { envelope: { header: { seq: number } } }) => listed(item.envelope, item.envelope.header.seq)),
    );
    const view = await readRequest(v.context, v.prepared);
    expect(view.state).toBe('answered');
    expect(view.expiresAt).toBe(Date.parse(EXPIRES));
    expect(view.responses.map((item) => item.status)).toEqual(['verified', 'verified']);
    VECTORS.responses.forEach((item: { body_json: string }, index: number) => {
      const opened = view.responses[index]!;
      expect(opened.status === 'verified' && opened.body).toEqual(JSON.parse(item.body_json));
    });
    expect(latestResponse(view)).toMatchObject({
      final: true,
      body: { step: 'done' },
      at: Date.parse('2026-11-21T12:00:00.000Z'),
    });
  });

  for (const vector of VECTORS.invalid.filter((item: { op: string }) => item.op.endsWith('response'))) {
    it(`keeps the forged response "${vector.name}" as unverifiable (${vector.error})`, async () => {
      const v = await vectorWorld(vector.args.user_kem_sk);
      v.serve([listed(vector.args.envelope, 1)]);
      const view = await readRequest(v.context, v.prepared);
      expect(view.responses).toEqual([{ seq: 1, status: 'unverifiable', code: vector.error }]);
      expect(latestResponse(view)).toBeNull();
    });
  }

  it('refuses a response listed under another seq, or for another request, user, device or kind', async () => {
    const v = await vectorWorld();
    const first = VECTORS.responses[0].envelope;
    v.serve([listed(first, 2)]);
    expect((await readRequest(v.context, v.prepared)).responses).toEqual([
      { seq: 2, status: 'unverifiable', code: 'header_invalid' },
    ]);
    for (const field of ['id', 'user', 'kind'] as const) {
      const other = {
        ...v.prepared,
        [field === 'id' ? 'id' : field]: field === 'kind' ? 'github.connect' : '01a1ee56-5e00-7000-8000-000000000000',
      };
      v.client.routes.clear();
      v.client.on(`GET /v1/device-requests/${other.id}`, () => ({
        request_id: other.id,
        kind: other.kind,
        device_id: other.device.device,
        state: 'pending',
        created_at: '2026-11-21T12:00:00.000Z',
        expires_at: EXPIRES,
        responses: [listed(first, 1)],
      }));
      if (field === 'user')
        setSession(v.store, '01a1ee56-5e00-7000-8000-000000000000', { keyId: 'k', expiresAt: T0 + 1 });
      const view = await readRequest(v.context, other as PreparedRequest);
      expect(view.responses[0]).toEqual({ seq: 1, status: 'unverifiable', code: 'header_invalid' });
    }
  });

  it('refuses a response whose body the kind does not allow, and malformed list items', async () => {
    const t = await taskWorld();
    const device = await actor('dev-x');
    const user = t.me;
    const header = {
      v: 1,
      id: '01a1ee56-5e00-7000-8000-0000000000f1',
      user: user.id,
      device: device.id,
      kind: 'github.connect',
      in_reply_to: '01a1ee56-5e00-7000-8000-0000000000f0',
      seq: 1,
      final: true,
      ts: '2026-11-21T12:00:00.000Z',
      enc_alg: 'HPKE-X25519-A256GCM',
    };
    const body = new TextEncoder().encode('{"step":"done","login":"bad login"}');
    const envelope = await sealResponse(header, user.identity.kemPublic(), body, device.identity.sig.privateKey);
    const prepared: PreparedRequest = {
      id: header.in_reply_to,
      kind: 'github.connect',
      follows: '',
      device: { device: device.id, ...device.identity.public() },
      envelope: { header: {}, ct: '', sig: '' },
    };
    t.client.on(`GET /v1/device-requests/${prepared.id}`, () => ({
      request_id: prepared.id,
      kind: 'github.connect',
      device_id: device.id,
      state: 'answered',
      created_at: '2026-11-21T12:00:00.000Z',
      expires_at: EXPIRES,
      responses: [listed(envelope, 1), { seq: 0, envelope }, 'junk'],
    }));
    const view = await readRequest(t.context, prepared);
    expect(view.responses).toEqual([
      { seq: 0, status: 'unverifiable', code: 'header_invalid' },
      { seq: 0, status: 'unverifiable', code: 'header_invalid' },
      { seq: 1, status: 'unverifiable', code: 'params_invalid' },
    ]);
  });

  it('refuses hub answers that do not describe the request', async () => {
    const v = await vectorWorld();
    for (const extra of [
      { request_id: 'other' },
      { kind: 'github.connect' },
      { device_id: 'other' },
      { state: 3 },
      { responses: null },
      { expires_at: 'soon' },
    ]) {
      v.client.routes.clear();
      v.serve([], extra);
      await expect(readRequest(v.context, v.prepared)).rejects.toMatchObject({ code: 'unavailable' });
    }
    v.client.routes.clear();
    v.client.on(`GET /v1/device-requests/${v.prepared.id}`, () => null);
    await expect(readRequest(v.context, v.prepared)).rejects.toMatchObject({ code: 'unavailable' });
  });

  it('reports responses as unverifiable after a sign-out', async () => {
    const v = await vectorWorld();
    v.serve([listed(VECTORS.responses[0].envelope, 1)]);
    v.store.reset();
    expect((await readRequest(v.context, v.prepared)).responses).toEqual([
      { seq: 1, status: 'unverifiable', code: 'header_invalid' },
    ]);
  });
});

/** The task world with an org device assigned to the default workgroup. */
async function withDevice(): Promise<TaskWorld & { device: Awaited<ReturnType<typeof actor>> }> {
  const t = await taskWorld();
  const device = await actor('org-device');
  await t.world.admitDevice(t.me, device, [t.wg]);
  setLog(t.store, t.org, t.world.log!);
  return { ...t, device };
}

describe('deviceKeys', () => {
  it('takes the keys of an eligible device from the verified log', async () => {
    const t = await withDevice();
    expect(deviceKeys(t.store, t.device.id)).toEqual({ device: t.device.id, ...t.device.identity.public() });
  });

  it('knows no user, no member, users, devices of blocked orgs or without a verified platform log', async () => {
    const t = await withDevice();
    expect(deviceKeys(t.store, t.me.id)).toBeNull();
    expect(deviceKeys(t.store, '01a1ee56-5e00-7000-8000-000000000000')).toBeNull();
    ensureOrg(t.store, 'unverified-org');
    blockOrg(t.store, t.org, { code: 'log_fork', stored: null, served: null, lh: null });
    expect(deviceKeys(t.store, t.device.id)).toBeNull();
    const anonymous = createStore();
    expect(deviceKeys(anonymous, t.device.id)).toBeNull();
  });

  it('needs the verified platform log for platform devices and refuses decertified ones', async () => {
    const t = await taskWorld();
    const [dev1] = t.devices;
    const { buildBatch } = await import('../../../src/core/log/rotation');
    const { formatTime } = await import('../../../src/core/encoding/time');
    const { epochKey } = await import('../../../src/core/log/state');
    const keys = t.world.held.get(t.me.id)!;
    const built = await buildBatch({
      log: t.world.log!,
      author: { id: t.me.id, identity: t.me.identity },
      entries: [{ type: 'device_assign', device: dev1!.id, wg: t.wg, platform_seq: t.platform.seqs.get(dev1!.id)! }],
      keys: (scope, scopeId, epoch) => keys.get(epochKey(scope, scopeId, epoch)) ?? null,
      platform: t.platform.state,
      ts: formatTime(T0),
    });
    setLog(t.store, t.org, built.log);
    expect(deviceKeys(t.store, dev1!.id)).toEqual({ device: dev1!.id, ...dev1!.identity.public() });
    const bare = createStore();
    setSession(bare, t.me.id, { keyId: 'k', expiresAt: T0 + 1 });
    ensureOrg(bare, t.org);
    setLog(bare, t.org, built.log);
    expect(deviceKeys(bare, dev1!.id)).toBeNull();
    const { buildPlatformEntries, entryHash } = await import('../../../src/core/log/entries');
    const { ed25519KeyPair } = await import('../../../src/core/crypto/keys');
    const { seed } = await import('../log/world');
    const root = await ed25519KeyPair(await seed('platform root'));
    const last = t.platform.entries.at(-1)!;
    const head = { seq: t.platform.entries.length - 1, hash: await entryHash(last) };
    const more = await buildPlatformEntries(
      head,
      root.privateKey,
      [{ type: 'device_decertify', device: dev1!.id }],
      formatTime(T0),
    );
    setPlatform(t.store, await verifyPlatformLog([...t.platform.entries, ...more], t.platform.root));
    expect(deviceKeys(t.store, dev1!.id)).toBeNull();
  });
});

describe('prepareRequest and submitRequest', () => {
  it('seals a request that the addressed device opens, with a follow-up chained to it', async () => {
    const t = await withDevice();
    const first = await prepareRequest(t.context, {
      device: t.device.id,
      kind: 'claude.login',
      body: { mode: 'manual' },
    });
    expect(first.envelope.header).toMatchObject({
      user: t.me.id,
      device: t.device.id,
      kind: 'claude.login',
      follows: '',
      ts: '2026-11-21T12:00:00.000Z',
    });
    const plain = await openRequest(t.device.identity.kem, first.envelope, t.me.identity.sigPublic());
    expect(fromUtf8(plain)).toBe('{"mode":"manual"}');
    const follow = await prepareRequest(t.context, {
      device: t.device.id,
      kind: 'claude.login',
      body: { code: 'abc' },
      follows: first,
    });
    expect(follow.follows).toBe(first.id);
    t.client.on('POST /v1/device-requests', (body) => ({
      request_id: (body as { envelope: { header: { id: string } } }).envelope.header.id,
      state: 'pending',
    }));
    expect(await submitRequest(t.context, first)).toBe('pending');
    expect(t.client.of('POST /v1/device-requests')[0]!.body).toEqual({ envelope: first.envelope });
  });

  it('refuses bodies the kind does not allow, mismatched follow-ups, unknown devices and anonymous users', async () => {
    const t = await withDevice();
    await expect(
      prepareRequest(t.context, { device: t.device.id, kind: 'github.connect', body: { x: 1 } }),
    ).rejects.toMatchObject({ code: 'params_invalid' });
    const first = await prepareRequest(t.context, { device: t.device.id, kind: 'github.connect', body: {} });
    await expect(
      prepareRequest(t.context, { device: t.device.id, kind: 'claude.login', body: { code: 'x' }, follows: first }),
    ).rejects.toMatchObject({ code: 'request_invalid' });
    await expect(
      prepareRequest(t.context, { device: t.me.id, kind: 'github.connect', body: {}, follows: first }),
    ).rejects.toMatchObject({ code: 'request_invalid' });
    await expect(
      prepareRequest(t.context, { device: t.me.id, kind: 'github.connect', body: {} }),
    ).rejects.toMatchObject({ code: 'device_not_assigned' });
    t.store.reset();
    await expect(
      prepareRequest(t.context, { device: t.device.id, kind: 'github.connect', body: {} }),
    ).rejects.toMatchObject({ code: 'unauthorized' });
  });

  it('refuses an answer for another request', async () => {
    const t = await withDevice();
    const first = await prepareRequest(t.context, { device: t.device.id, kind: 'github.connect', body: {} });
    t.client.on('POST /v1/device-requests', () => ({ request_id: 'other', state: 'pending' }));
    await expect(submitRequest(t.context, first)).rejects.toMatchObject({ code: 'unavailable' });
  });
});

describe('eligibleDevices', () => {
  it('lists the hub devices and refuses malformed lists', async () => {
    const t = await taskWorld();
    const item = { device_id: 'd', kind: 'platform', org_id: t.org, wg_ids: [t.wg], online: true, last_seen_at: null };
    t.client.on('GET /v1/me/devices', () => ({ devices: [item] }));
    expect(await eligibleDevices(t.context)).toEqual([item]);
    for (const bad of [
      null,
      { devices: null },
      { devices: [{ ...item, wg_ids: [1] }] },
      { devices: [{ ...item, last_seen_at: 1 }] },
    ]) {
      t.client.on('GET /v1/me/devices', () => bad);
      await expect(eligibleDevices(t.context)).rejects.toMatchObject({ code: 'unavailable' });
    }
  });
});

describe('requestErrorCode', () => {
  it('names the code of request, hub and crypto errors and unavailable for anything else', () => {
    expect(requestErrorCode(new RequestError('busy'))).toBe('busy');
    expect(requestErrorCode(new HubError('forbidden', 403))).toBe('forbidden');
    expect(requestErrorCode(new CryptoError('decrypt_failed'))).toBe('decrypt_failed');
    expect(requestErrorCode(new Error('x'))).toBe('unavailable');
  });
});
