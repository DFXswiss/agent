// GitHub identities (ADR-076, KM-35): own records verified and opened in the browser (only the shown part leaves),
// company records by metadata only, the changes (primary, delete, defaults, the task's identity), the devices of a
// `peer_outdated`, storing a company account under a fresh epoch of the organization's credential key that only its
// devices can open, re-sealing own further records under the newest CK, and the views of the task choice.
import { describe, expect, it } from 'vitest';
import { parseJson } from '../../../src/core/encoding/json';
import { openCredential } from '../../../src/core/crypto/credential';
import { Secret } from '../../../src/core/crypto/secret';
import type { Sealed } from '../../../src/core/crypto/signed';
import { unwrapKey } from '../../../src/core/crypto/wrap';
import {
  accountOf,
  companyBody,
  fetchIdentity,
  type GithubIdentity,
  identityName,
  listIdentities,
  loadIdentities,
  makePrimary,
  openIdentity,
  outdatedDevices,
  removeIdentity,
  rewrapOwnIdentities,
  setGithubDefault,
  setTaskIdentity,
  storeCompanyIdentity,
  usableIdentities,
} from '../../../src/core/github/identities';
import { HubError } from '../../../src/core/net/signedFetch';
import type { RequestView } from '../../../src/core/requests/client';
import { putTasks } from '../../../src/core/store/slices/tasks';
import { setLog } from '../../../src/core/store/slices/orgs';
import { actor, TS } from '../log/world';
import { taskRow, taskWorld } from '../tasks/world';
import { selfWrap } from '../credentials/world';
import { BODY, company, COMPANY, own, OWN, SECOND, sealed, world } from './identity-hub';

describe('listIdentities and fetchIdentity', () => {
  it('reads the metadata and the defaults', async () => {
    const { t, hub } = await world();
    company(hub, COMPANY);
    hub.defaults = [{ scope: 'org', scope_id: t.org, identity_id: COMPANY, set_by: t.me.id, set_at: TS }];
    expect(await listIdentities(t.context)).toEqual({
      identities: [
        {
          id: COMPANY,
          ownerId: t.org,
          ownerKind: 'org',
          primary: false,
          version: 1,
          epoch: 1,
          createdAt: TS,
          updatedAt: TS,
        },
      ],
      defaults: [{ scope: 'org', scopeId: t.org, identityId: COMPANY, setBy: t.me.id, setAt: TS }],
    });
  });

  it('refuses a malformed list', async () => {
    const { t } = await world();
    const meta = { id: 'a', owner_id: 'b', owner_kind: 'user', primary: true, version: 1, epoch: 1, created_at: TS };
    const cases: unknown[] = [
      null,
      { identities: null, defaults: [] },
      { identities: [{ ...meta }], defaults: [] },
      { identities: [{ ...meta, updated_at: TS, owner_kind: 'team' }], defaults: [] },
      { identities: [{ ...meta, updated_at: TS, version: 0 }], defaults: [] },
      { identities: [], defaults: null },
      { identities: [], defaults: [{ scope: 'task', scope_id: 'x', identity_id: 'y', set_by: 'z', set_at: TS }] },
      { identities: [], defaults: [{ scope: 'wg', scope_id: 'x', identity_id: 'y', set_by: 'z' }] },
      { identities: [null], defaults: [] },
    ];
    for (const body of cases) {
      t.client.on('GET /v1/me/github-identities', () => body);
      await expect(listIdentities(t.context)).rejects.toMatchObject({ code: 'unavailable' });
    }
  });

  it('treats a vanished record as none and passes other errors on', async () => {
    const { t } = await world();
    expect(await fetchIdentity(t.context, OWN)).toBeNull();
    t.client.request = async () => {
      throw new HubError('unavailable', 503);
    };
    await expect(fetchIdentity(t.context, OWN)).rejects.toMatchObject({ code: 'unavailable' });
  });
});

describe('openIdentity and accountOf', () => {
  it('opens an own record written by the user or an entitled device', async () => {
    const { t } = await world();
    const keys = [{ epoch: 1, key: t.credKey, createdAt: null }];
    for (const author of [t.me, t.device]) {
      const record = await sealed(t, { author });
      const opened = await openIdentity(t.context, OWN, { owner_id: t.me.id, owner_kind: 'user', record }, keys);
      expect(opened.body).toEqual(BODY);
      expect(opened.header.version).toBe(1);
    }
  });

  it('refuses foreign, mismatching and forged records', async () => {
    const { t } = await world();
    const stranger = await actor('stranger');
    const keys = [{ epoch: 1, key: t.credKey, createdAt: null }];
    const answer = (record: unknown, extra: Record<string, unknown> = {}) => ({
      owner_id: t.me.id,
      owner_kind: 'user',
      record,
      ...extra,
    });
    const good = await sealed(t);
    const cases: [unknown, string][] = [
      [null, 'header_invalid'],
      [answer(good, { owner_id: t.org }), 'header_invalid'],
      [answer(good, { owner_kind: 'org' }), 'header_invalid'],
      [answer(await sealed(t, { id: SECOND })), 'header_invalid'],
      [answer(await sealed(t, { owner: t.org })), 'header_invalid'],
      [answer(await sealed(t, { kind: 'mailbox' })), 'header_invalid'],
      [answer({ header: 'x' }), 'header_invalid'],
      [answer(await sealed(t, { author: stranger })), 'author_not_writer'],
      [answer(await sealed(t, { epoch: 2 })), 'epoch_stale'],
      [answer(await sealed(t, { body: { ...BODY, token: '' } })), 'params_invalid'],
      [answer(await sealed(t, { body: { ...BODY, created_at: 'yesterday' } })), 'header_invalid'],
    ];
    for (const [value, code] of cases) {
      await expect(openIdentity(t.context, OWN, value, keys)).rejects.toMatchObject({ code });
    }
  });

  it('needs a signed-in user', async () => {
    const { t } = await world();
    t.store.reset();
    await expect(openIdentity(t.context, OWN, null, [])).rejects.toMatchObject({ code: 'unauthorized' });
  });

  it('shows only the login, name, origin, scopes, key presence and times', () => {
    expect(accountOf(BODY)).toEqual({
      login: 'octo-cat',
      name: null,
      source: 'device_flow',
      scopes: ['repo'],
      githubUserId: 42,
      signingKey: false,
      connectedAt: BODY.created_at,
      revocable: false,
    });
    const key = { alg: 'ssh-ed25519' as const, seed: 'A'.repeat(43), public: `ssh-ed25519 ${'A'.repeat(68)}` };
    expect(
      accountOf({ ...BODY, token: 'ghu_x', name: 'Octo Cat', source: 'operator', signing_key: key }),
    ).toMatchObject({ name: 'Octo Cat', source: 'operator', signingKey: true, revocable: true });
  });
});

describe('loadIdentities', () => {
  it('opens own records, keeps company records as metadata and stores the result', async () => {
    const { t, hub } = await world();
    own(hub, OWN, await sealed(t), { primary: true });
    own(hub, SECOND, await sealed(t, { id: SECOND, epoch: 2 }));
    company(hub, COMPANY);
    hub.defaults = [{ scope: 'wg', scope_id: t.wg, identity_id: COMPANY, set_by: t.me.id, set_at: TS }];
    const loaded = await loadIdentities(t.context);
    expect(loaded.map((item) => [item.id, item.status])).toEqual([
      [OWN, 'verified'],
      [SECOND, 'unverifiable'],
      [COMPANY, 'company'],
    ]);
    expect(loaded[1]).toMatchObject({ code: 'epoch_stale' });
    const state = t.store.getState().githubIdentities;
    expect(state).toMatchObject({ status: 'loaded', identities: loaded, knownLogins: {} });
    expect(state.defaults).toEqual([{ scope: 'wg', scopeId: t.wg, identityId: COMPANY, setBy: t.me.id, setAt: TS }]);
    expect(JSON.stringify(state)).not.toContain('gho_');
  });

  it('reads no keys without own records and skips records that vanished', async () => {
    const { t, hub } = await world();
    company(hub, COMPANY);
    expect(await loadIdentities(t.context)).toHaveLength(1);
    expect(t.client.of('GET /v1/wraps')).toHaveLength(0);
    own(hub, OWN, null);
    t.client.on('GET /v1/me/github-identities', () => {
      const list = hub.list();
      hub.records.delete(OWN);
      return list;
    });
    expect((await loadIdentities(t.context)).map((item) => item.id)).toEqual([COMPANY]);
  });

  it('keeps the code of anything else that fails', async () => {
    const { t, hub } = await world();
    const broken = {};
    Object.defineProperty(broken, 'header', {
      get() {
        throw new Error('broken');
      },
      enumerable: true,
    });
    own(hub, OWN, broken);
    expect(await loadIdentities(t.context)).toEqual([expect.objectContaining({ code: 'unavailable' })]);
  });

  it('writes nothing after a sign-out and needs a signed-in user', async () => {
    const { t, hub } = await world();
    company(hub, COMPANY);
    t.client.on('GET /v1/me/github-identities', () => {
      t.store.reset();
      return hub.list();
    });
    await loadIdentities(t.context);
    expect(t.store.getState().githubIdentities.status).toBe('unknown');
    await expect(loadIdentities(t.context)).rejects.toMatchObject({ code: 'unauthorized' });
  });
});

describe('changes', () => {
  it('makes a record primary and removes records', async () => {
    const { t } = await world();
    t.client.request = async (method: string, target: string, options: { body?: unknown } = {}) => {
      t.client.calls.push({ method, target, body: options.body });
      if (target.endsWith('/gone')) throw new HubError('not_found', 404);
      if (target.endsWith('/broken')) throw new HubError('unavailable', 503);
      return {};
    };
    await makePrimary(t.context, OWN);
    await removeIdentity(t.context, OWN);
    await removeIdentity(t.context, 'gone');
    await expect(removeIdentity(t.context, 'broken')).rejects.toMatchObject({ code: 'unavailable' });
    expect(t.client.calls.slice(0, 2)).toEqual([
      { method: 'PUT', target: `/v1/github-identities/${OWN}/primary`, body: {} },
      { method: 'DELETE', target: `/v1/github-identities/${OWN}`, body: undefined },
    ]);
  });

  it('sets and clears defaults', async () => {
    const { t } = await world();
    t.client.on(`PUT /v1/github-defaults/org/${t.org}`, () => ({}));
    t.client.on(`PUT /v1/github-defaults/wg/${t.wg}`, () => ({}));
    await setGithubDefault(t.context, 'org', t.org, COMPANY);
    await setGithubDefault(t.context, 'wg', t.wg, null);
    expect(t.client.calls.map((call) => call.body)).toEqual([{ identity_id: COMPANY }, { identity_id: null }]);
  });

  it("sets the task's identity and updates a known row", async () => {
    const { t } = await world();
    const row = taskRow(t);
    const task = row.task_id;
    t.client.on(`PUT /v1/tasks/${task}/github-identity`, () => ({}));
    await setTaskIdentity(t.context, task, COMPANY);
    expect(t.store.getState().tasks.tasks[task]).toBeUndefined();
    putTasks(t.store, [row]);
    await setTaskIdentity(t.context, task, COMPANY);
    expect(t.store.getState().tasks.tasks[task]?.github_identity).toBe(COMPANY);
    await setTaskIdentity(t.context, task, null);
    expect(t.store.getState().tasks.tasks[task]?.github_identity).toBeNull();
    t.client.on(`PUT /v1/tasks/${task}/github-identity`, () => {
      t.store.reset();
      return {};
    });
    putTasks(t.store, [row]);
    await setTaskIdentity(t.context, task, COMPANY);
    expect(t.store.getState().tasks.tasks[task]).toBeUndefined();
  });

  it('names the devices of a peer_outdated', () => {
    expect(outdatedDevices(new Error('x'))).toBeNull();
    expect(outdatedDevices(new HubError('forbidden', 403, { devices: ['d'] }))).toBeNull();
    expect(outdatedDevices(new HubError('peer_outdated', 409))).toBeNull();
    expect(outdatedDevices(new HubError('peer_outdated', 409, { devices: 'd' }))).toBeNull();
    expect(outdatedDevices(new HubError('peer_outdated', 409, { devices: [] }))).toBeNull();
    expect(outdatedDevices(new HubError('peer_outdated', 409, { devices: [1] }))).toBeNull();
    expect(outdatedDevices(new HubError('peer_outdated', 409, { devices: ['d1', 'd2'] }))).toEqual(['d1', 'd2']);
  });
});

describe('company accounts', () => {
  const view = (responses: RequestView['responses']): RequestView => ({
    id: 'r',
    state: 'answered',
    expiresAt: 0,
    responses,
  });

  it('takes the body of the final answer only', () => {
    const done = { step: 'done' as const, login: 'octo-cat' };
    expect(companyBody(view([]))).toBeNull();
    expect(companyBody(view([{ seq: 1, status: 'verified', final: true, at: 0, body: done }]))).toBeNull();
    expect(
      companyBody(view([{ seq: 1, status: 'verified', final: true, at: 0, body: { step: 'failed', code: 'busy' } }])),
    ).toBeNull();
    expect(
      companyBody(
        view([
          { seq: 1, status: 'verified', final: true, at: 0, body: { ...done, body: BODY } },
          { seq: 2, status: 'unverifiable', code: 'x' },
        ]),
      ),
    ).toEqual(BODY);
  });

  it('seals a new account under a fresh epoch that only the devices can open', async () => {
    const { t, hub } = await world();
    company(hub, COMPANY, { epoch: 3 });
    const stored = await storeCompanyIdentity(t.context, t.org, BODY);
    expect(stored.version).toBe(1);
    expect(stored.id).not.toBe(COMPANY);
    const put = hub.puts.at(-1)!;
    const key = put.body.key as { epoch: number; wraps: Record<string, unknown>[] };
    expect(key.epoch).toBe(4);
    expect(put.body).toMatchObject({ owner_id: t.org, expected_version: 0 });
    expect(key.wraps.map((wrap) => wrap.recipient_id)).toEqual([t.device.id]);
    const wrap = key.wraps[0]!;
    expect(wrap).toMatchObject({ scope: 'cred', scope_id: t.org, epoch: 4 });
    const opened = await unwrapKey(
      t.device.identity.kem,
      { scope: 'cred', scope_id: t.org, epoch: 4, recipient: t.device.id },
      wrap.wrap as string,
    );
    const record = put.body.record as Sealed;
    expect(record.header).toMatchObject({ id: stored.id, owner: t.org, author: t.me.id, epoch: 4, version: 1 });
    expect(parseJson(await openCredential(opened, record, t.me.identity.sigPublic()))).toEqual(BODY);
    expect(t.store.getState().githubIdentities.knownLogins).toEqual({ [stored.id]: 'octo-cat' });
  });

  it('reconnects into the same record and starts at epoch 1 for a first account', async () => {
    const { t, hub } = await world();
    company(hub, COMPANY, { version: 2, epoch: 1 });
    expect(await storeCompanyIdentity(t.context, t.org, BODY, COMPANY)).toEqual({ id: COMPANY, version: 3 });
    expect(hub.puts.at(-1)!.body).toMatchObject({ expected_version: 2, key: { epoch: 2 } });
    await expect(storeCompanyIdentity(t.context, t.org, BODY, SECOND)).rejects.toMatchObject({ code: 'not_found' });
    hub.records.clear();
    await storeCompanyIdentity(t.context, t.org, BODY);
    expect(hub.puts.at(-1)!.body).toMatchObject({ key: { epoch: 1 } });
  });

  it('retries once after a concurrent write and passes other failures on', async () => {
    const { t, hub } = await world();
    hub.failPut = [new HubError('epoch_stale', 409)];
    await storeCompanyIdentity(t.context, t.org, BODY);
    expect(hub.puts).toHaveLength(2);
    hub.failPut = [new HubError('version_conflict', 409), new HubError('version_conflict', 409)];
    await expect(storeCompanyIdentity(t.context, t.org, BODY)).rejects.toMatchObject({ code: 'version_conflict' });
    hub.failPut = [new HubError('forbidden', 403)];
    await expect(storeCompanyIdentity(t.context, t.org, BODY)).rejects.toMatchObject({ code: 'forbidden' });
    hub.answerVersion = 7;
    await expect(storeCompanyIdentity(t.context, t.org, BODY)).rejects.toMatchObject({ code: 'unavailable' });
  });

  it('takes the epoch the hub expects when the record itself did not change', async () => {
    const { t, hub } = await world();
    // the record with the newest epoch was deleted: the scope's next epoch (6) is above every listed record's (3)
    company(hub, COMPANY, { epoch: 3 });
    hub.failPut = [new HubError('version_conflict', 409, { expected: 6 })];
    await storeCompanyIdentity(t.context, t.org, BODY);
    expect(hub.puts.map((put) => (put.body.key as { epoch: number }).epoch)).toEqual([4, 6]);
    // an expected epoch below the listed one, or none usable, keeps the listed one
    for (const detail of [{ expected: 2 }, { expected: 0 }, { expected: 2 ** 31 }, { expected: 1.5 }, 'x', null]) {
      hub.puts.length = 0;
      hub.records.clear();
      company(hub, COMPANY, { epoch: 3 });
      hub.failPut = [new HubError('version_conflict', 409, detail)];
      await storeCompanyIdentity(t.context, t.org, BODY);
      expect(hub.puts.map((put) => (put.body.key as { epoch: number }).epoch)).toEqual([4, 4]);
    }
  });

  it('ignores the expected value when the record was written in between, and passes a later refusal on', async () => {
    const { t, hub } = await world();
    company(hub, COMPANY, { version: 2, epoch: 1 });
    const fallback = t.client.request;
    let first = true;
    t.client.request = async (method: string, target: string, options: { body?: unknown } = {}) => {
      if (method === 'PUT' && first) {
        first = false;
        // another admin reconnected the record meanwhile: its version is now 3 (the hub's expected version)
        hub.records.set(COMPANY, { ...hub.records.get(COMPANY)!, version: 3, epoch: 2 });
        throw new HubError('version_conflict', 409, { expected: 2 });
      }
      return fallback(method, target, options);
    };
    expect(await storeCompanyIdentity(t.context, t.org, BODY, COMPANY)).toEqual({ id: COMPANY, version: 4 });
    expect(hub.puts.at(-1)!.body).toMatchObject({ expected_version: 3, key: { epoch: 3 } });
    t.client.request = fallback;
    hub.failPut = [new HubError('version_conflict', 409), new HubError('forbidden', 403)];
    await expect(storeCompanyIdentity(t.context, t.org, BODY)).rejects.toMatchObject({ code: 'forbidden' });
  });

  it('refuses a malformed body, non-admins, an unverified org and an org without devices', async () => {
    const { t } = await world();
    await expect(storeCompanyIdentity(t.context, t.org, { ...BODY, login: '' })).rejects.toMatchObject({
      code: 'params_invalid',
    });
    await expect(storeCompanyIdentity(t.context, 'other-org', BODY)).rejects.toMatchObject({ code: 'forbidden' });
    const member = await world({ member: true });
    await expect(storeCompanyIdentity(member.t.context, member.t.org, BODY)).rejects.toMatchObject({
      code: 'forbidden',
    });
    const bare = await taskWorld();
    await expect(storeCompanyIdentity(bare.context, bare.org, BODY)).rejects.toMatchObject({
      code: 'no_device_available',
    });
    t.store.reset();
    await expect(storeCompanyIdentity(t.context, t.org, BODY)).rejects.toMatchObject({ code: 'unauthorized' });
  });

  it('keeps no login after a sign-out during the write', async () => {
    const { t, hub } = await world();
    hub.answerVersion = null;
    const fallback = t.client.request;
    t.client.request = async (method: string, target: string, options: { body?: unknown } = {}) => {
      const answer = await fallback(method, target, options);
      if (method === 'PUT') t.store.reset();
      return answer;
    };
    await storeCompanyIdentity(t.context, t.org, BODY);
    expect(t.store.getState().githubIdentities.knownLogins).toEqual({});
  });
});

describe('rewrapOwnIdentities', () => {
  async function rotated() {
    const { t, hub } = await world();
    const key2 = Secret.of(new Uint8Array(32).fill(8));
    t.hub.wraps.push(await selfWrap(t, 2, key2, null));
    return { t, hub, key2 };
  }

  it('re-seals own further records of an older epoch under the newest key', async () => {
    const { t, hub, key2 } = await rotated();
    own(hub, OWN, await sealed(t), { primary: true });
    own(hub, SECOND, await sealed(t, { id: SECOND, author: t.device, version: 3 }), { version: 3 });
    own(hub, COMPANY, await sealed(t, { id: COMPANY, epoch: 2, key: key2 }), { epoch: 2 });
    expect(await rewrapOwnIdentities(t.context)).toBe(1);
    const put = hub.puts.at(-1)!;
    expect(put.id).toBe(SECOND);
    expect(put.body).toMatchObject({ owner_id: t.me.id, expected_version: 3 });
    expect((put.body.record as Sealed).header).toMatchObject({ epoch: 2, version: 4, author: t.me.id });
    expect(parseJson(await openCredential(key2, put.body.record, t.me.identity.sigPublic()))).toEqual(BODY);
    expect(put.body).not.toHaveProperty('key');
  });

  it('leaves records it cannot open, vanished ones and concurrent writes alone', async () => {
    const { t, hub } = await rotated();
    const stranger = await actor('stranger');
    own(hub, OWN, await sealed(t, { author: stranger }));
    own(hub, SECOND, await sealed(t, { id: SECOND }));
    hub.failPut = [new HubError('version_conflict', 409)];
    expect(await rewrapOwnIdentities(t.context)).toBe(0);
    const fallback = t.client.request;
    t.client.request = async (method: string, target: string, options: { body?: unknown } = {}) => {
      if (method === 'GET' && target.endsWith(SECOND)) throw new HubError('not_found', 404);
      return fallback(method, target, options);
    };
    expect(await rewrapOwnIdentities(t.context)).toBe(0);
    t.client.request = fallback;
    hub.failPut = [new HubError('unavailable', 503)];
    await expect(rewrapOwnIdentities(t.context)).rejects.toMatchObject({ code: 'unavailable' });
  });

  it('does nothing without further records, without keys or signed out', async () => {
    const { t, hub } = await world();
    own(hub, OWN, await sealed(t), { primary: true });
    company(hub, COMPANY);
    expect(await rewrapOwnIdentities(t.context)).toBe(0);
    expect(t.client.of('GET /v1/wraps')).toHaveLength(0);
    own(hub, SECOND, await sealed(t, { id: SECOND }));
    t.hub.wraps.length = 0;
    expect(await rewrapOwnIdentities(t.context)).toBe(0);
    t.store.reset();
    expect(await rewrapOwnIdentities(t.context)).toBe(0);
  });
});

describe('views', () => {
  const meta = { ownerKind: 'user' as const, primary: false, version: 1, epoch: 1, createdAt: TS, updatedAt: TS };
  const account = accountOf(BODY);
  const identities: GithubIdentity[] = [
    { ...meta, id: 'c1', ownerId: 'org-1', ownerKind: 'org', status: 'company' },
    { ...meta, id: 'u1', ownerId: 'me', status: 'verified', account },
    { ...meta, id: 'u2', ownerId: 'me', status: 'unverifiable', code: 'epoch_stale' },
    { ...meta, id: 'x1', ownerId: 'someone', status: 'verified', account },
    { ...meta, id: 'c2', ownerId: 'org-2', ownerKind: 'org', status: 'company' },
  ];

  it('offers own verified accounts, then the company accounts of the org', () => {
    expect(usableIdentities(identities, 'me', 'org-1').map((item) => item.id)).toEqual(['u1', 'c1']);
    expect(usableIdentities(identities, null, 'org-3')).toEqual([]);
  });

  it('names an identity by login, as a company account or as another account', () => {
    expect(identityName(identities, {}, 'u1')).toEqual({ kind: 'login', login: 'octo-cat' });
    expect(identityName(identities, {}, 'c1')).toEqual({ kind: 'company', org: 'org-1', login: null });
    expect(identityName(identities, { c1: 'acme-bot' }, 'c1')).toEqual({
      kind: 'company',
      org: 'org-1',
      login: 'acme-bot',
    });
    expect(identityName(identities, {}, 'u2')).toEqual({ kind: 'other' });
    expect(identityName(identities, {}, 'nope')).toEqual({ kind: 'other' });
  });
});

describe('an organization with a removed device', () => {
  it('wraps only for the active assigned devices', async () => {
    const { t, hub } = await world();
    const spare = await actor('spare-device');
    await t.world.admitDevice(t.me, spare, [t.wg]);
    await t.world.batch(t.me, [{ type: 'remove', subject: spare.id }]);
    setLog(t.store, t.org, t.world.log!);
    await storeCompanyIdentity(t.context, t.org, BODY);
    const key = hub.puts.at(-1)!.body.key as { wraps: { recipient_id: string }[] };
    expect(key.wraps.map((wrap) => wrap.recipient_id)).toEqual([t.device.id]);
  });
});
