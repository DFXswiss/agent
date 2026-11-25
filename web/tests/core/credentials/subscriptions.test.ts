// U-WEB-credentials (KOR-69, ADR-090): Claude subscriptions in the browser – the list with each record verified and
// opened (only label, note, address, status, use and the reading leave), the devices that can log one in, the login
// request of a further subscription, the owner's changes sealed again with the mirror of the use, and removal.
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openRequest } from '../../../src/core/crypto/request';
import {
  fetchSubscription,
  isSubscriptionRecord,
  isUse,
  loadSubscriptions,
  logoutSubscription,
  newSubscriptionId,
  noteSubscription,
  openSubscription,
  readingOf,
  removeSubscription,
  renameSubscription,
  setSubscriptionUse,
  startSubscriptionLogin,
  subscriptionDevices,
} from '../../../src/core/credentials/subscriptions';
import { isLabel, validRequestBody } from '../../../src/core/requests/bodies';
import { HubError } from '../../../src/core/net/signedFetch';
import { actor, TS } from '../log/world';
import { sealed } from '../github/identity-hub';
import { A, B, body, C, ORG, reading, TOKEN, world } from './sub-hub';

describe('bodies', () => {
  it('accepts the bodies of further subscriptions and their uses', () => {
    expect(isSubscriptionRecord(body({ name: 'Team', note: 'n', use: { pool: true, orgs: [ORG] } }))).toBe(true);
    expect(isSubscriptionRecord({ email: null, kind: 'setup_token' })).toBe(true);
    expect(isSubscriptionRecord({ ...body(), expires_at: TS, kind: 'login_token' })).toBe(true);
    for (const bad of [
      null,
      { email: null },
      { ...body(), extra: 1 },
      { ...body(), kind: 'x' },
      { ...body(), email: 'no' },
      { ...body(), token: 'a b' },
      { ...body(), expires_at: 1 },
      { ...body(), name: '' },
      { ...body(), note: 'n'.repeat(501) },
      { ...body(), use: { pool: true } },
    ])
      expect(isSubscriptionRecord(bad)).toBe(false);
    expect(isUse({ pool: false, orgs: [ORG, ORG] })).toBe(false);
    expect(isUse({ pool: false, orgs: ['x'] })).toBe(false);
    expect(isLabel('a\nb', 64)).toBe(false);
    expect(validRequestBody('claude.login', { mode: 'manual', subscription_id: A, name: 'N', note: 'x' }, false)).toBe(
      true,
    );
    expect(validRequestBody('claude.login', { mode: 'manual', email: 'a@example.com' }, false)).toBe(false);
    expect(validRequestBody('claude.login', { mode: 'automatic', email: 'a@example.com' }, false)).toBe(true);
    expect(validRequestBody('claude.login', { mode: 'manual', name: '' }, false)).toBe(false);
    expect(validRequestBody('claude.login', { mode: 'manual', note: '' }, false)).toBe(false);
  });

  it('reads the weekly and the five-hour window of a reading', () => {
    expect(readingOf(null)).toBeNull();
    expect(readingOf({ fetched_at: TS })).toBeNull();
    const found = readingOf({
      fetched_at: TS,
      device_id: ORG,
      windows: [{ window: 'five_hour', used_percent: 3, resets_at: null }],
    });
    expect(found).toEqual({
      measuredAt: TS,
      deviceId: ORG,
      unreadable: false,
      weekPercent: null,
      weekResetsAt: null,
      windowPercent: 3,
      windowResetsAt: null,
    });
    expect(readingOf({ fetched_at: TS, device_id: ORG, windows: 'x' })?.weekPercent).toBeNull();
  });
});

describe('loadSubscriptions', () => {
  it('opens every record, never returns the token and sorts by room', async () => {
    const { t, hub } = await world();
    await hub.add(A, body({ name: 'Team', note: 'reviews', use: { pool: false, orgs: [ORG] } }), {
      reading: reading(40),
    });
    await hub.add(B, body(), { primary: true, reading: reading(10) });
    await hub.add(C, { email: null, kind: 'setup_token', name: 'Out' });
    const unread = '0190f000-0000-7000-8000-0000000000c8';
    await hub.add(unread, body());
    const gone = '0190f000-0000-7000-8000-0000000000c9';
    const found = await loadSubscriptions(t.context);
    expect(found.map((item) => [item.id, item.status, item.name, item.use])).toEqual([
      [B, 'logged_in', null, null],
      [A, 'logged_in', 'Team', { pool: false, orgs: [ORG] }],
      [C, 'not_logged_in', 'Out', { pool: false, orgs: [] }],
      [unread, 'logged_in', null, { pool: false, orgs: [] }],
    ]);
    expect(found[1]!.note).toBe('reviews');
    expect(found[1]!.reading?.weekPercent).toBe(40);
    expect(JSON.stringify(found)).not.toContain(TOKEN);
    await hub.add(gone, body(), { reading: reading(null, true) });
    const listed = hub.rows.get(gone)!;
    t.client.on('GET /v1/me/subscriptions', () => ({
      subscriptions: [{ id: gone, owner_id: t.me.id, primary: false, version: 1, epoch: 1, reading: listed.reading }],
    }));
    expect((await loadSubscriptions(t.context))[0]!.status).toBe('not_readable');
    hub.rows.delete(gone);
    expect(await loadSubscriptions(t.context)).toEqual([]);
  });

  it('keeps records that do not open as unverifiable', async () => {
    const { t, hub } = await world();
    await hub.add(A, body(), { owner: '0190f000-0000-7000-8000-0000000000ee' });
    await hub.add(B, body({ use: { pool: true } }));
    await hub.add(C, body(), { author: await actor('stranger') });
    const stale = '0190f000-0000-7000-8000-0000000000c5';
    await hub.add(stale, body());
    hub.rows.get(stale)!.record = await sealed(t, { id: stale, kind: 'claude_subscription', body: body(), epoch: 9 });
    const wrongKey = '0190f000-0000-7000-8000-0000000000c4';
    await hub.add(wrongKey, body());
    const tampered = hub.rows.get(wrongKey)!.record as { ct: string };
    tampered.ct = (tampered.ct.startsWith('A') ? 'B' : 'A') + tampered.ct.slice(1);
    const codes = Object.fromEntries((await loadSubscriptions(t.context)).map((item) => [item.id, item.code]));
    expect(codes[A]).toBe('header_invalid');
    expect(codes[B]).toBe('params_invalid');
    expect(codes[C]).toBe('author_not_writer');
    expect(codes[wrongKey]).toBe('unverifiable');
    expect(codes[stale]).toBe('epoch_stale');
  });

  it('refuses a malformed list and an unknown user', async () => {
    const { t } = await world();
    t.client.on('GET /v1/me/subscriptions', () => ({ subscriptions: [{ id: 1 }] }));
    await expect(loadSubscriptions(t.context)).rejects.toMatchObject({ code: 'unavailable' });
    t.client.on('GET /v1/me/subscriptions', () => ({ subscriptions: [] }));
    t.store.update('me', (me) => ({ ...me, userId: null }));
    await expect(loadSubscriptions(t.context)).rejects.toMatchObject({ code: 'unauthorized' });
  });

  it('refuses an answer that is no record', async () => {
    const { t } = await world();
    await expect(openSubscription(t.context, A, null, [])).rejects.toMatchObject({ code: 'header_invalid' });
    const answer = { owner_id: t.me.id, record: 'x' };
    await expect(openSubscription(t.context, A, answer, [])).rejects.toMatchObject({ code: 'header_invalid' });
  });

  it('passes other errors of a read on', async () => {
    const { t, hub } = await world();
    hub.handle = () => {
      throw new HubError('forbidden', 403);
    };
    await expect(fetchSubscription(t.context, A)).rejects.toMatchObject({ code: 'forbidden' });
  });
});

describe('the login', () => {
  it('chooses an online device that announced subscriptions and sends the subscription', async () => {
    const { t } = await world();
    const item = (features: string[], online: boolean) => ({
      device_id: t.device.id,
      kind: 'platform',
      org_id: t.org,
      wg_ids: [t.wg],
      online,
      last_seen_at: null,
      features,
    });
    const { features: _none, ...bare } = item([], true);
    t.client.on('GET /v1/me/devices', () => ({ devices: [item([], true), bare] }));
    expect((await subscriptionDevices(t.context)).preselected).toBeNull();
    t.client.on('GET /v1/me/devices', () => ({ devices: [item(['subscriptions'], false)] }));
    expect(await subscriptionDevices(t.context)).toMatchObject({ preselected: null });
    t.client.on('GET /v1/me/devices', () => ({ devices: [item(['subscriptions'], true)] }));
    expect((await subscriptionDevices(t.context)).preselected).toBe(t.device.id);
    t.client.on('POST /v1/device-requests', (sent) => ({
      request_id: (sent as { envelope: { header: { id: string } } }).envelope.header.id,
      state: 'pending',
    }));
    const id = newSubscriptionId();
    await startSubscriptionLogin(t.context, t.device.id, {
      subscriptionId: id,
      mode: 'manual',
      name: ' Team ',
      note: '',
    });
    await startSubscriptionLogin(t.context, t.device.id, {
      subscriptionId: id,
      mode: 'automatic',
      email: 'a@example.com',
    });
    const bodies = [];
    for (const call of t.client.of('POST /v1/device-requests')) {
      const envelope = (call.body as { envelope: never }).envelope;
      const plain = await openRequest(t.device.identity.kem, envelope, b64uDecode(t.me.identity.public().sig));
      bodies.push(JSON.parse(fromUtf8(plain)));
    }
    expect(bodies).toEqual([
      { mode: 'manual', subscription_id: id, name: 'Team' },
      { mode: 'automatic', subscription_id: id, email: 'a@example.com' },
    ]);
  });
});

describe('changes', () => {
  it('renames, notes, sets the use and logs out, each sealed again with the mirror of its use', async () => {
    const { t, hub } = await world();
    await hub.add(A, body({ name: 'Old', note: 'n' }));
    await renameSubscription(t.context, A, ' New ');
    expect((await hub.opened(A)).name).toBe('New');
    expect(hub.puts.at(-1)!.body).toMatchObject({ expected_version: 1, use: { pool: false, orgs: [] } });
    await noteSubscription(t.context, A, 'for reviews');
    expect((await hub.opened(A)).note).toBe('for reviews');
    await noteSubscription(t.context, A, ' ');
    expect('note' in (await hub.opened(A))).toBe(false);
    await setSubscriptionUse(t.context, A, { pool: false, orgs: [ORG, ORG] });
    expect((await hub.opened(A)).use).toEqual({ pool: false, orgs: [ORG] });
    expect(hub.puts.at(-1)!.body.use).toEqual({ pool: false, orgs: [ORG] });
    await setSubscriptionUse(t.context, A, { pool: true, orgs: [] });
    expect((await hub.opened(A)).use).toEqual({ pool: true, orgs: [] });
    await setSubscriptionUse(t.context, A, null);
    expect('use' in (await hub.opened(A))).toBe(false);
    await logoutSubscription(t.context, A);
    expect(await hub.opened(A)).toEqual({ email: 'team@example.com', kind: 'setup_token', name: 'New' });
  });

  it('refuses bad input, the primary record and gone records, and retries one concurrent write', async () => {
    const { t, hub } = await world();
    await hub.add(A, body());
    await hub.add(B, body(), { primary: true });
    expect(() => renameSubscription(t.context, A, '')).toThrow();
    expect(() => noteSubscription(t.context, A, 'n'.repeat(501))).toThrow();
    expect(() => setSubscriptionUse(t.context, A, { pool: false, orgs: ['x'] })).toThrow();
    await expect(renameSubscription(t.context, B, 'x')).rejects.toMatchObject({ code: 'invalid_argument' });
    await expect(renameSubscription(t.context, C, 'x')).rejects.toMatchObject({ code: 'not_found' });
    hub.failPut = [new HubError('version_conflict', 409)];
    await renameSubscription(t.context, A, 'Again');
    expect((await hub.opened(A)).name).toBe('Again');
    hub.failPut = [new HubError('epoch_stale', 409), new HubError('version_conflict', 409)];
    await expect(renameSubscription(t.context, A, 'x')).rejects.toMatchObject({ code: 'version_conflict' });
    hub.failPut = [new HubError('forbidden', 403)];
    await expect(renameSubscription(t.context, A, 'x')).rejects.toMatchObject({ code: 'forbidden' });
    await hub.add(C, body({ use: { pool: false, orgs: [ORG] } }));
    hub.rows.get(C)!.record = { header: {} };
    await expect(renameSubscription(t.context, C, 'x')).rejects.toMatchObject({ code: 'header_invalid' });
    t.store.update('me', (me) => ({ ...me, userId: null }));
    await expect(renameSubscription(t.context, A, 'x')).rejects.toMatchObject({ code: 'unauthorized' });
  });

  it('removes a subscription, a gone one too', async () => {
    const { t, hub } = await world();
    await hub.add(A, body());
    await removeSubscription(t.context, A);
    await removeSubscription(t.context, A);
    expect(hub.rows.size).toBe(0);
    hub.failDelete = new HubError('forbidden', 403);
    await expect(removeSubscription(t.context, A)).rejects.toMatchObject({ code: 'forbidden' });
  });
});
