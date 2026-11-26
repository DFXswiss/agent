// U-WEB-subscriptions (KOR-68, rule E-30; data of KOR-69, ADR-090): "My subscriptions" – the list of
// `GET /v1/me/subscriptions` checked item by item, every `claude_subscription` record fetched and opened like an own
// record (header, author, epoch), a record that does not open shown with its code, a record the hub no longer has left
// out; the operator subscriptions of the token files for the holder of the platform role `operator` only (once per
// label, the newest read wins); the provider groups, the colour levels and the time parts.
import { describe, expect, it } from 'vitest';
import { ed25519KeyPair } from '../../../src/core/crypto/keys';
import { buildPlatformEntries } from '../../../src/core/log/entries';
import { appendPlatform } from '../../../src/core/log/platform';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import { setIdentity, setProfile } from '../../../src/core/store/slices/me';
import { setPlatform } from '../../../src/core/store/slices/orgs';
import {
  ageParts,
  bindingOf,
  exhaustedBy,
  freeLevel,
  freeOf,
  FULLEST,
  KIND,
  listedItems,
  loadSubscriptions,
  OPERATOR_ROLE,
  openedBody,
  ordered,
  providerGroups,
  providerName,
  type QuotaWindow,
  reading,
  type SubscriptionRow,
  totalWindows,
  untilParts,
  usableNow,
} from '../../../src/core/subscriptions/subscriptions';
import { credWorld, type CredWorld, storedRecord, SUBSCRIPTION } from '../credentials/world';
import { actor, idOf, seed } from '../log/world';

const AT = '2026-11-24T13:05:00.000Z';
const LATER = '2026-11-24T14:05:00.000Z';
const EARLIER = '2026-11-24T12:05:00.000Z';
const ms = Date.parse;
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';

const id = (n: number) => `0190f000-0000-7000-8000-0000000005${String(n).padStart(2, '0')}`;

const win = (name: string, used: number, resets: string | null = LATER) => ({
  window: name,
  used_percent: used,
  resets_at: resets,
  severity: null,
});

const measured = (windows: unknown[], overrides: Record<string, unknown> = {}) => ({
  fetched_at: AT,
  device_id: DEVICE,
  unreadable: false,
  windows,
  ...overrides,
});

/** An item of `GET /v1/me/subscriptions`. */
const item = (n: number, overrides: Record<string, unknown> = {}) => ({
  id: id(n),
  owner_id: 'x',
  primary: false,
  version: 1,
  epoch: 1,
  created_at: AT,
  updated_at: AT,
  reading: null,
  ...overrides,
});

const codeOf = (run: () => unknown) => {
  try {
    run();
  } catch (error) {
    return (error as { code: string }).code;
  }
  return 'none';
};

/** Serves `record` at `GET /v1/subscriptions/{id}`. */
function serve(t: CredWorld, n: number, answer: () => unknown) {
  t.client.on(`GET /v1/subscriptions/${id(n)}`, answer);
}

async function serveRecord(
  t: CredWorld,
  n: number,
  body: unknown,
  options: Parameters<typeof storedRecord>[3] = {},
  primary = false,
) {
  const record = await storedRecord(t, KIND, body, { id: id(n), ...options });
  serve(t, n, () => ({ owner_id: t.me.id, primary, record }));
  return record;
}

describe('parsing', () => {
  it('reads a reading and its windows in the order 5 hours, week, week per model', () => {
    expect(reading('x')).toBeNull();
    expect(reading({ fetched_at: 5, windows: [] })).toBeNull();
    expect(reading({ fetched_at: 'soon', windows: [] })).toBeNull();
    expect(reading({ fetched_at: AT, windows: 'x' })).toBeNull();
    const found = reading(
      measured([
        win('weekly:sonnet', 3),
        win('seven_day', 50, null),
        win('weekly:opus', 4),
        win('five_hour', 20),
        'x',
        { ...win('monthly', 1) },
        { ...win('five_hour', 1), window: 5 },
        win('five_hour', 'full' as never),
        win('five_hour', -1),
        win('five_hour', Infinity),
        win('five_hour', 1, 'soon'),
        { window: 'five_hour', used_percent: 1 },
      ]),
    );
    expect(found).toEqual({
      fetchedAt: ms(AT),
      deviceId: DEVICE,
      unreadable: false,
      windows: [
        { window: 'five_hour', usedPercent: 20, resetsAt: ms(LATER) },
        { window: 'seven_day', usedPercent: 50, resetsAt: null },
        { window: 'weekly:opus', usedPercent: 4, resetsAt: ms(LATER) },
        { window: 'weekly:sonnet', usedPercent: 3, resetsAt: ms(LATER) },
      ],
    });
    expect(reading({ fetched_at: AT, device_id: null, unreadable: true, windows: [] })).toEqual({
      fetchedAt: ms(AT),
      deviceId: null,
      unreadable: true,
      windows: [],
    });
    const windows = (names: string[]) => names.map((name) => ({ window: name, usedPercent: 1, resetsAt: null }));
    expect(
      ordered(windows(['weekly:b', 'weekly:a', 'seven_day', 'weekly:a', 'five_hour'])).map((w) => w.window),
    ).toEqual(['five_hour', 'seven_day', 'weekly:a', 'weekly:a', 'weekly:b']);
  });

  it('refuses a malformed list and leaves malformed items out', () => {
    expect(codeOf(() => listedItems(null))).toBe('unavailable');
    expect(codeOf(() => listedItems({ subscriptions: 'x' }))).toBe('unavailable');
    const found = listedItems({
      subscriptions: [
        'x',
        item(1, { id: 1 }),
        item(2, { primary: 'yes' }),
        item(3, { primary: true, reading: measured([]) }),
        item(4, { updated_at: 'never' }),
      ],
    });
    expect(found).toEqual([
      {
        id: id(3),
        primary: true,
        reading: { fetchedAt: ms(AT), deviceId: DEVICE, unreadable: false, windows: [] },
        updatedAt: ms(AT),
      },
      { id: id(4), primary: false, reading: null, updatedAt: null },
    ]);
  });

  it('reads the opened body leniently', () => {
    expect(codeOf(() => openedBody('x'))).toBe('params_invalid');
    expect(
      openedBody({ ...SUBSCRIPTION, name: 'Team', note: 'for the night', use: { pool: true, orgs: ['a', 1] } }),
    ).toEqual({
      name: 'Team',
      note: 'for the night',
      email: 'claude@example.com',
      token: true,
      use: { pool: true, orgs: ['a'] },
    });
    expect(openedBody({ token: '', name: '', note: 'n'.repeat(501), email: 5, use: 'x' })).toEqual({
      name: null,
      note: null,
      email: null,
      token: false,
      use: null,
    });
    expect(openedBody({ token: 5, name: 'n'.repeat(65), use: { pool: 'yes', orgs: [] } })).toMatchObject({
      name: null,
      token: false,
      use: null,
    });
    expect(openedBody({ use: { pool: false, orgs: 'x' } }).use).toBeNull();
  });
});

describe('loadSubscriptions', () => {
  it('opens every record, shows one that does not open with its code and leaves out one the hub no longer has', async () => {
    const t = await credWorld();
    const other = await idOf('org other');
    setProfile(t.store, {
      orgs: [
        { org_id: t.org, role: 'admin', private: true, default_wg: t.wg },
        { org_id: other, role: 'member', private: false, default_wg: t.wg },
      ],
    });
    // the log of the further org cannot be read: the records are still opened
    t.context.refreshLog.mockRejectedValueOnce(new Error('offline'));
    const stranger = await actor('stranger');
    const list = [
      item(1, { reading: measured([win('five_hour', 20), win('seven_day', 50)]) }),
      item(2, { primary: true, reading: measured([win('seven_day', 80)]) }),
      item(3),
      item(4, { reading: measured([], { unreadable: true }) }),
      item(5),
      item(6),
      item(7),
      item(8),
      item(9),
      item(10),
      item(11),
      item(12),
      item(13),
      item(14),
      item(15, { updated_at: LATER }),
    ];
    t.client.on('GET /v1/me/subscriptions', () => ({ subscriptions: list }));
    // 1: a further subscription, logged in, measured, with its use
    await serveRecord(t, 1, { ...SUBSCRIPTION, name: 'Team', note: 'night', use: { pool: true, orgs: [t.org] } });
    // 2: the primary record, written by the device that logged it in
    await serveRecord(t, 2, SUBSCRIPTION, { author: t.device }, true);
    // 3: not logged in (no token), without use
    await serveRecord(t, 3, { kind: 'setup_token', email: null, name: 'Spare' });
    // 4: logged in, the usage endpoint refuses the token
    await serveRecord(t, 4, { ...SUBSCRIPTION, name: 'Beta' });
    // 5: logged in, not measured yet, same name as 4
    await serveRecord(t, 5, { ...SUBSCRIPTION, name: 'Beta' });
    // 6: gone at the hub (skipped); 7: refused otherwise
    serve(t, 6, () => {
      throw new HubError('not_found', 404);
    });
    serve(t, 7, () => {
      throw new HubError('forbidden', 403);
    });
    // 8: no answer the client understands
    serve(t, 8, () => {
      throw new Error('broken');
    });
    // 9–13: the header does not belong here
    serve(t, 9, () => 'x');
    serve(t, 10, () => ({ record: 'x' }));
    await serveRecord(t, 11, SUBSCRIPTION, { id: id(1) });
    const foreign = await storedRecord(t, KIND, SUBSCRIPTION, { id: id(12) });
    serve(t, 12, () => ({ record: { ...foreign, header: { ...foreign.header, owner: stranger.id } } }));
    const github = await storedRecord(t, 'github', SUBSCRIPTION, { id: id(13) });
    serve(t, 13, () => ({ record: github }));
    // 14: written by somebody who may not; 15: under a CK of an epoch the user has no key of
    await serveRecord(t, 14, SUBSCRIPTION, { author: stranger });
    await serveRecord(t, 15, SUBSCRIPTION, { epoch: 2 });

    const rows = await loadSubscriptions(t.context);
    expect(t.context.refreshLog).toHaveBeenCalledTimes(1);
    expect(t.context.refreshLog).toHaveBeenCalledWith(other);
    const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));
    expect(rows.map((row) => row.key)).toEqual([
      id(2),
      id(7),
      id(8),
      id(9),
      id(10),
      id(11),
      id(12),
      id(13),
      id(14),
      id(15),
      id(4),
      id(5),
      id(3),
      id(1),
    ]);
    expect(byKey[id(2)]).toEqual({
      key: id(2),
      provider: 'claude',
      source: 'record',
      primary: true,
      name: null,
      note: null,
      email: 'claude@example.com',
      status: 'logged_in',
      code: null,
      use: 'own',
      state: null,
      reading: { fetchedAt: ms(AT), deviceId: DEVICE, unreadable: false, windows: [expect.anything()] },
      changedAt: ms(AT),
    });
    expect(byKey[id(1)]).toMatchObject({
      name: 'Team',
      note: 'night',
      status: 'logged_in',
      use: { pool: true, orgs: [t.org] },
    });
    expect(byKey[id(3)]).toMatchObject({
      name: 'Spare',
      email: null,
      status: 'not_logged_in',
      use: { pool: false, orgs: [] },
    });
    expect(byKey[id(4)]).toMatchObject({ status: 'not_readable' });
    expect(byKey[id(5)]).toMatchObject({ status: 'logged_in', reading: null });
    const codes = Object.fromEntries(
      rows.filter((row) => row.status === 'unverifiable').map((row) => [row.key, row.code]),
    );
    expect(codes).toEqual({
      [id(7)]: 'forbidden',
      [id(8)]: 'unavailable',
      [id(9)]: 'header_invalid',
      [id(10)]: 'header_invalid',
      [id(11)]: 'header_invalid',
      [id(12)]: 'header_invalid',
      [id(13)]: 'header_invalid',
      [id(14)]: 'author_not_writer',
      [id(15)]: 'epoch_stale',
    });
    expect(byKey[id(15)]).toMatchObject({ name: null, email: null, use: null, changedAt: ms(LATER) });
    expect(byKey[id(6)]).toBeUndefined();
  });

  it('opens nothing without a session or an unlocked identity', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/me/subscriptions', () => ({ subscriptions: [item(1)] }));
    await serveRecord(t, 1, SUBSCRIPTION);
    setIdentity(t.store, null);
    expect((await loadSubscriptions(t.context)).map((row) => [row.status, row.code])).toEqual([
      ['unverifiable', 'unauthorized'],
    ]);
    const context = { ...t.context, store: createStore() };
    expect((await loadSubscriptions(context)).map((row) => row.code)).toEqual(['unauthorized']);
  });

  it('reads no operator subscriptions without the platform log or the role', async () => {
    const empty = { subscriptions: [] };
    const without = await credWorld({ noPlatform: true });
    without.client.on('GET /v1/me/subscriptions', () => empty);
    expect(await loadSubscriptions(without.context)).toEqual([]);
    const t = await credWorld();
    t.client.on('GET /v1/me/subscriptions', () => empty);
    expect(await loadSubscriptions(t.context)).toEqual([]);
    expect(t.client.of(`GET /v1/orgs/${t.org}/quotas`)).toHaveLength(0);
    expect(t.context.refreshLog).not.toHaveBeenCalled();
  });

  it('reads the operator subscriptions of the granted organizations once per label, the newest read', async () => {
    const t = await credWorld();
    const [second, refused] = await Promise.all([idOf('org second'), idOf('org refused')]);
    const root = await ed25519KeyPair(await seed('platform root'));
    const fresh = await appendPlatform(null, t.platform.entries, t.platform.root);
    const entries = await buildPlatformEntries(t.platform.state.headValue(), root.privateKey, [
      { type: 'role_grant', subject: t.me.id, role: OPERATOR_ROLE },
      { type: 'org_operator_subscriptions', org: t.org, granted: true },
      { type: 'org_operator_subscriptions', org: second, granted: true },
      { type: 'org_operator_subscriptions', org: refused, granted: false },
    ]);
    setPlatform(t.store, await appendPlatform(fresh, entries, t.platform.root));
    setProfile(t.store, {
      orgs: [t.org, second, refused].map((org) => ({ org_id: org, role: 'admin', private: false, default_wg: t.wg })),
    });
    t.client.on('GET /v1/me/subscriptions', () => ({ subscriptions: [] }));
    const sub = (label: string, fetched: string | null, used: number | null, readable = true) => ({
      label,
      state: 'usable',
      readable,
      used_percent: used,
      resets_at: used === null ? null : LATER,
      limited_at: null,
      until: null,
      fetched_at: fetched,
    });
    const quotas = (subscriptions: unknown[]) => ({
      own: { windows: [], unreadable: null, limited: null },
      operator: { granted: true, subscriptions },
      platform: { available: true },
      summary: { usable: 0, exhausted: 0, next_release: null },
    });
    t.client.on(`GET /v1/orgs/${t.org}/quotas`, () =>
      quotas([sub('d', AT, 10), sub('a', AT, 40), sub('b', null, null, false), sub('c', LATER, null)]),
    );
    t.client.on(`GET /v1/orgs/${second}/quotas`, () =>
      quotas([sub('a', LATER, 70), sub('b', AT, 5), sub('c', EARLIER, 99), sub('d', null, null)]),
    );
    const rows = await loadSubscriptions(t.context);
    expect(t.client.of(`GET /v1/orgs/${refused}/quotas`)).toHaveLength(0);
    expect(rows.map((row) => [row.key, row.status, row.reading?.windows ?? null])).toEqual([
      ['operator:a', 'measured', [{ window: FULLEST, usedPercent: 70, resetsAt: ms(LATER) }]],
      ['operator:b', 'measured', [{ window: FULLEST, usedPercent: 5, resetsAt: ms(LATER) }]],
      ['operator:c', 'measured', []],
      ['operator:d', 'measured', [{ window: FULLEST, usedPercent: 10, resetsAt: ms(LATER) }]],
    ]);
    expect(rows[0]).toEqual({
      key: 'operator:a',
      provider: 'claude',
      source: 'operator',
      primary: false,
      name: 'a',
      note: null,
      email: null,
      status: 'measured',
      code: null,
      use: { pool: true, orgs: [] },
      state: 'usable',
      reading: { fetchedAt: ms(LATER), deviceId: null, unreadable: false, windows: expect.anything() },
      changedAt: null,
    });
    // not readable and never read: no reading
    t.client.on(`GET /v1/orgs/${second}/quotas`, () => quotas([]));
    const again = await loadSubscriptions(t.context);
    expect(again.find((row) => row.key === 'operator:b')).toMatchObject({ status: 'not_readable', reading: null });
  });
});

describe('loadSubscriptions (review KOR-68)', () => {
  it('lists a repeated ID once and shows a record with a forged signature as "cannot be opened" with its reading', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/me/subscriptions', () => ({
      subscriptions: [item(1, { reading: measured([win('seven_day', 130)]) }), item(1), item(2)],
    }));
    const record = (await serveRecord(t, 1, SUBSCRIPTION)) as { sig: string };
    serve(t, 1, () => ({ owner_id: t.me.id, primary: false, record: { ...record, sig: 'A'.repeat(86) } }));
    await serveRecord(t, 2, SUBSCRIPTION);
    const rows = await loadSubscriptions(t.context);
    expect(rows.map((row) => [row.key, row.status])).toEqual([
      [id(1), 'unverifiable'],
      [id(2), 'logged_in'],
    ]);
    expect(rows[0]!.code).not.toBeNull();
    // more than 100 % is kept as measured (the bar is full, the figure says it)
    expect(rows[0]!.reading?.windows).toEqual([{ window: 'seven_day', usedPercent: 130, resetsAt: ms(LATER) }]);
  });

  it('shows the readings when the own keys cannot be read', async () => {
    const t = await credWorld();
    t.client.on('GET /v1/me/subscriptions', () => ({
      subscriptions: [item(1, { reading: measured([win('seven_day', 40)]) })],
    }));
    await serveRecord(t, 1, SUBSCRIPTION);
    t.client.on('GET /v1/wraps', () => {
      throw new HubError('unavailable', 503);
    });
    const rows = await loadSubscriptions(t.context);
    expect(rows.map((row) => [row.status, row.code, row.reading?.windows.length])).toEqual([
      ['unverifiable', 'epoch_stale', 1],
    ]);
  });

  it('leaves out an organization whose quotas cannot be read, never the others', async () => {
    const t = await credWorld();
    const [refusing, malformed] = await Promise.all([idOf('org refusing'), idOf('org malformed')]);
    const root = await ed25519KeyPair(await seed('platform root'));
    const fresh = await appendPlatform(null, t.platform.entries, t.platform.root);
    const entries = await buildPlatformEntries(t.platform.state.headValue(), root.privateKey, [
      { type: 'role_grant', subject: t.me.id, role: OPERATOR_ROLE },
      ...[t.org, refusing, malformed].map((org) => ({ type: 'org_operator_subscriptions', org, granted: true })),
    ]);
    setPlatform(t.store, await appendPlatform(fresh, entries, t.platform.root));
    setProfile(t.store, {
      orgs: [refusing, malformed, t.org].map((org) => ({
        org_id: org,
        role: 'admin',
        private: false,
        default_wg: t.wg,
      })),
    });
    t.client.on('GET /v1/me/subscriptions', () => ({ subscriptions: [] }));
    t.client.on(`GET /v1/orgs/${refusing}/quotas`, () => {
      throw new HubError('forbidden', 403);
    });
    t.client.on(`GET /v1/orgs/${malformed}/quotas`, () => ({ own: null }));
    t.client.on(`GET /v1/orgs/${t.org}/quotas`, () => ({
      own: { windows: [], unreadable: null, limited: null },
      operator: {
        granted: true,
        subscriptions: [
          {
            label: 'a',
            state: 'usable',
            readable: true,
            used_percent: 30,
            resets_at: LATER,
            limited_at: null,
            until: null,
            fetched_at: AT,
          },
        ],
      },
      platform: { available: true },
      summary: { usable: 1, exhausted: 0, next_release: null },
    }));
    expect((await loadSubscriptions(t.context)).map((row) => row.key)).toEqual(['operator:a']);
  });
});

describe('token files named like an own record', () => {
  it('lists only the record, with the reading of the file when the record has none', async () => {
    const t = await credWorld();
    const root = await ed25519KeyPair(await seed('platform root'));
    const fresh = await appendPlatform(null, t.platform.entries, t.platform.root);
    const entries = await buildPlatformEntries(t.platform.state.headValue(), root.privateKey, [
      { type: 'role_grant', subject: t.me.id, role: OPERATOR_ROLE },
      { type: 'org_operator_subscriptions', org: t.org, granted: true },
    ]);
    setPlatform(t.store, await appendPlatform(fresh, entries, t.platform.root));
    setProfile(t.store, { orgs: [{ org_id: t.org, role: 'admin', private: false, default_wg: t.wg }] });
    t.client.on('GET /v1/me/subscriptions', () => ({
      subscriptions: [item(1, { reading: measured([win('seven_day', 12)]) }), item(2), item(3)],
    }));
    await serveRecord(t, 1, { ...SUBSCRIPTION, name: 'Kuno' });
    await serveRecord(t, 2, { ...SUBSCRIPTION, name: 'Abo5' });
    await serveRecord(t, 3, { ...SUBSCRIPTION });
    const file = (label: string, used: number) => ({
      label,
      state: 'usable',
      readable: true,
      used_percent: used,
      resets_at: LATER,
      limited_at: null,
      until: null,
      fetched_at: AT,
    });
    t.client.on(`GET /v1/orgs/${t.org}/quotas`, () => ({
      own: { windows: [], unreadable: null, limited: null },
      operator: { granted: true, subscriptions: [file('kuno', 90), file('ABO5', 40), file('other', 5)] },
      platform: { available: true },
      summary: { usable: 3, exhausted: 0, next_release: null },
    }));
    const rows = await loadSubscriptions(t.context);
    expect(rows.map((row) => [row.key, row.source, row.reading?.windows[0]?.usedPercent ?? null])).toEqual([
      [id(3), 'record', null],
      [id(2), 'record', 40],
      [id(1), 'record', 12],
      ['operator:other', 'operator', 5],
    ]);
  });
});

const rowOf = (key: string, provider: string, windows: QuotaWindow[] | null, fetchedAt = ms(AT)): SubscriptionRow => ({
  key,
  provider,
  source: 'record',
  primary: false,
  name: null,
  note: null,
  email: null,
  status: 'logged_in',
  code: null,
  use: null,
  state: null,
  reading: windows === null ? null : { fetchedAt, deviceId: null, unreadable: false, windows },
  changedAt: null,
});
const w = (name: string, used: number): QuotaWindow => ({ window: name, usedPercent: used, resetsAt: null });

describe('providers', () => {
  it('names a provider, an unknown one by its ID', () => {
    expect(providerName('claude')).toBe('Claude');
    expect(providerName('openai')).toBe('ChatGPT');
    expect(providerName('mistral')).toBe('mistral');
  });

  it('shows what is left: 100 % less the used share, never below 0 %', () => {
    expect([0, 13, 82, 99.5, 100, 130].map(freeOf)).toEqual([100, 87, 18, 0.5, 0, 0]);
  });

  describe('the window that binds an account (least left)', () => {
    const bound = (windows: QuotaWindow[] | null) => {
      const found = bindingOf(rowOf('a', 'claude', windows));
      return found === null ? null : [found.binding.window, found.binding.usedPercent, found.other?.window ?? null];
    };

    it('is the 5-hour window when it is used up, the week stays as the other value', () => {
      // 5 hours at 100 % (0 % left), week at 13 % (87 % left): not free
      expect(bound([w('five_hour', 100), w('seven_day', 13)])).toEqual(['five_hour', 100, 'seven_day']);
      expect(bindingOf(rowOf('a', 'claude', [w('five_hour', 100), w('seven_day', 13)]))?.other?.usedPercent).toBe(13);
    });

    it('is the week when it is used up; a week per model never binds the account', () => {
      expect(bound([w('five_hour', 4), w('seven_day', 100)])).toEqual(['seven_day', 100, 'five_hour']);
      expect(bound([w('seven_day', 80), w('five_hour', 79.9), w('weekly:opus', 99)])).toEqual([
        'seven_day',
        80,
        'five_hour',
      ]);
    });

    it('is the week on a tie of what is left, also beyond 100 %', () => {
      expect(bound([w('five_hour', 100), w('seven_day', 100)])).toEqual(['seven_day', 100, 'five_hour']);
      expect(bound([w('five_hour', 0), w('seven_day', 0)])).toEqual(['seven_day', 0, 'five_hour']);
      // both 0 % left
      expect(bound([w('five_hour', 130), w('seven_day', 100)])).toEqual(['seven_day', 100, 'five_hour']);
    });

    it('takes the one that is there when the other is missing, else the fullest window', () => {
      expect(bound(null)).toBeNull();
      expect(bound([])).toBeNull();
      expect(bound([w('five_hour', 100)])).toEqual(['five_hour', 100, null]);
      expect(bound([w('seven_day', 13), w('weekly:opus', 90)])).toEqual(['seven_day', 13, null]);
      expect(bound([w(FULLEST, 40)])).toEqual([FULLEST, 40, null]);
      expect(bound([w('weekly:x', 60), w('weekly:y', 5)])).toEqual(['weekly:x', 60, null]);
    });
  });

  it('marks an account with nothing left in its binding window, with the time of its release', () => {
    const at = (name: string, used: number, resets: string | null): QuotaWindow => ({
      window: name,
      usedPercent: used,
      resetsAt: resets === null ? null : ms(resets),
    });
    const row = (status: SubscriptionRow['status'], windows: QuotaWindow[]) => ({
      ...rowOf('a', 'claude', windows),
      status,
    });
    expect(exhaustedBy(row('logged_in', [at('five_hour', 100, LATER), at('seven_day', 13, null)]))).toEqual(
      at('five_hour', 100, LATER),
    );
    expect(exhaustedBy(row('logged_in', [at('five_hour', 99, LATER), at('seven_day', 13, null)]))).toBeNull();
    // a week per model with nothing left does not block the account
    expect(exhaustedBy(row('logged_in', [at('seven_day', 13, null), at('weekly:opus', 100, LATER)]))).toBeNull();
    // only for an account that could be used otherwise
    expect(exhaustedBy(row('not_logged_in', [at('five_hour', 100, LATER)]))).toBeNull();
    expect(exhaustedBy(row('measured', [at(FULLEST, 100, null)]))).toEqual(at(FULLEST, 100, null));
  });

  it('names the next release of a provider: the earliest reset of an account with nothing left', () => {
    const at = (name: string, used: number, resets: string | null): QuotaWindow => ({
      window: name,
      usedPercent: used,
      resetsAt: resets === null ? null : ms(resets),
    });
    const group = (rows: SubscriptionRow[]) => providerGroups(rows)[0]!;
    expect(
      group([
        rowOf('a', 'claude', [at('five_hour', 100, LATER), at('seven_day', 13, EARLIER)]),
        rowOf('b', 'claude', [at('five_hour', 10, null), at('seven_day', 100, AT)]),
        // usable: its earlier reset does not count
        rowOf('c', 'claude', [at('five_hour', 50, EARLIER), at('seven_day', 50, EARLIER)]),
      ]).nextRelease,
    ).toBe(ms(AT));
    // nothing used up, or the reset is unknown: no release to name
    expect(group([rowOf('a', 'claude', [at('seven_day', 50, AT)])]).nextRelease).toBeNull();
    expect(group([rowOf('a', 'claude', [at('seven_day', 100, null)])]).nextRelease).toBeNull();
  });

  it('counts an account as usable now when its binding window has something left', () => {
    const as = (status: SubscriptionRow['status'], windows: QuotaWindow[] | null) => ({
      ...rowOf('a', 'claude', windows),
      status,
    });
    expect(usableNow(as('logged_in', [w('five_hour', 100), w('seven_day', 13)]))).toBe(false);
    expect(usableNow(as('logged_in', [w('five_hour', 99.9), w('seven_day', 13)]))).toBe(true);
    expect(usableNow(as('logged_in', [w('five_hour', 10), w('seven_day', 100)]))).toBe(false);
    expect(usableNow(as('logged_in', [w('five_hour', 140)]))).toBe(false);
    expect(usableNow(as('measured', [w(FULLEST, 40)]))).toBe(true);
    expect(usableNow(as('measured', [w(FULLEST, 100)]))).toBe(false);
    // nothing measured, not logged in or not readable: not usable
    expect(usableNow(as('logged_in', null))).toBe(false);
    expect(usableNow(as('logged_in', []))).toBe(false);
    expect(usableNow(as('not_logged_in', [w('seven_day', 10)]))).toBe(false);
    expect(usableNow(as('not_readable', [w('seven_day', 10)]))).toBe(false);
    const group = providerGroups([
      as('logged_in', [w('five_hour', 100), w('seven_day', 13)]),
      { ...as('logged_in', [w('five_hour', 30), w('seven_day', 40)]), key: 'b' },
      { ...as('logged_in', [w('seven_day', 100)]), key: 'c' },
      { ...as('logged_in', null), key: 'd' },
    ])[0]!;
    expect(group.usable).toBe(1);
    expect(group.rows).toHaveLength(4);
    // the provider line stays with the mean per window: (87 + 60 + 0) / 3 left
    expect(group.headline).toMatchObject({ window: 'seven_day', usedPercent: 51, freePercent: 49, accounts: 3 });
  });

  it('totals a window over several accounts: the mean, every account weighted equally', () => {
    const totals = totalWindows([
      rowOf('a', 'claude', [w('seven_day', 99), w('five_hour', 40)], ms(LATER)),
      rowOf('b', 'claude', [w('seven_day', 60), w('five_hour', 10)], ms(AT)),
      rowOf('c', 'claude', [w('seven_day', 81), w('weekly:opus', 30)], ms(LATER)),
    ]);
    expect(totals).toEqual([
      { window: 'five_hour', usedPercent: 25, freePercent: 75, resetsAt: null, accounts: 2, measuredAt: ms(AT) },
      { window: 'seven_day', usedPercent: 80, freePercent: 20, resetsAt: null, accounts: 3, measuredAt: ms(AT) },
      { window: 'weekly:opus', usedPercent: 30, freePercent: 70, resetsAt: null, accounts: 1, measuredAt: ms(LATER) },
    ]);
    // what is left is the mean of each account's share left, so a reading beyond 100 % counts as 0 % left
    expect(
      totalWindows([rowOf('a', 'claude', [w('seven_day', 130)]), rowOf('b', 'claude', [w('seven_day', 90)])])[0],
    ).toMatchObject({ usedPercent: 110, freePercent: 5 });
    // nine accounts, one of them nearly used up: the total, not the fullest account
    const nine = Array.from({ length: 9 }, (_, n) => rowOf(`s${n}`, 'claude', [w('seven_day', n === 0 ? 99 : 80)]));
    expect(totalWindows(nine)[0]).toMatchObject({ window: 'seven_day', accounts: 9 });
    expect(totalWindows(nine)[0]!.usedPercent).toBeCloseTo((99 + 8 * 80) / 9);
  });

  it('leaves an account without the window out of its total', () => {
    const totals = totalWindows([
      rowOf('a', 'claude', [w('five_hour', 90), w('seven_day', 20)]),
      rowOf('b', 'claude', [w('seven_day', 40)]),
      rowOf('c', 'claude', [w(FULLEST, 70)]),
      rowOf('d', 'claude', [], ms(LATER)),
    ]);
    expect(totals.map((total) => [total.window, total.usedPercent, total.accounts])).toEqual([
      ['five_hour', 90, 1],
      ['seven_day', 30, 2],
      [FULLEST, 70, 1],
    ]);
    // a window repeated in one reading counts once for that account
    expect(totalWindows([rowOf('a', 'claude', [w('seven_day', 10), w('seven_day', 90)])])[0]).toMatchObject({
      usedPercent: 10,
      accounts: 1,
    });
  });

  it('has no total when nothing is measured', () => {
    expect(totalWindows([])).toEqual([]);
    expect(totalWindows([rowOf('a', 'claude', null), rowOf('b', 'claude', [])])).toEqual([]);
    expect(providerGroups([rowOf('a', 'claude', null), rowOf('b', 'claude', [])])[0]).toMatchObject({
      headline: null,
      headlineAt: null,
      windows: [],
    });
  });

  it('takes the time to the next reset of the counted accounts', () => {
    const reset = (name: string, used: number, at: string | null): QuotaWindow => ({
      window: name,
      usedPercent: used,
      resetsAt: at === null ? null : ms(at),
    });
    const totals = totalWindows([
      rowOf('a', 'claude', [reset('seven_day', 50, LATER), reset('five_hour', 10, null)]),
      rowOf('b', 'claude', [reset('seven_day', 50, EARLIER)]),
      rowOf('c', 'claude', [reset('seven_day', 50, null), reset('five_hour', 10, null)]),
      // a reset of an account without the window does not count
      rowOf('d', 'claude', [reset('weekly:opus', 50, '2026-11-24T00:00:00.000Z')]),
    ]);
    expect(totals.find((total) => total.window === 'seven_day')?.resetsAt).toBe(ms(EARLIER));
    expect(totals.find((total) => total.window === 'five_hour')?.resetsAt).toBeNull();
  });

  it('groups by provider with the total per window, by ID or least left first', () => {
    const rows = [
      rowOf('c1', 'claude', [w('five_hour', 30), w('seven_day', 50)], ms(AT)),
      rowOf('c2', 'claude', [w('seven_day', 80), w('five_hour', 10)], ms(LATER)),
      rowOf('c3', 'claude', [w(FULLEST, 99)], ms(EARLIER)),
      rowOf('o1', 'openai', null),
      rowOf('z1', 'zeta', [w(FULLEST, 95)]),
      rowOf('y1', 'yota', [w('seven_day', 100)]),
      rowOf('y2', 'yota', [w('seven_day', 20)]),
      rowOf('a1', 'alpha', null),
    ];
    const groups = providerGroups(rows);
    expect(groups.map((group) => group.provider)).toEqual(['alpha', 'claude', 'openai', 'yota', 'zeta']);
    const claude = groups[1]!;
    expect(claude).toMatchObject({
      name: 'Claude',
      headline: { window: 'seven_day', usedPercent: 65, freePercent: 35, accounts: 2 },
      windows: [
        { window: 'five_hour', usedPercent: 20, freePercent: 80, accounts: 2 },
        { window: 'seven_day', usedPercent: 65, freePercent: 35, accounts: 2 },
        { window: FULLEST, usedPercent: 99, freePercent: 1, accounts: 1 },
      ],
      newest: ms(LATER),
      // the oldest reading the headline counts (c3 has no week)
      headlineAt: ms(AT),
    });
    expect(claude.rows.map((found) => found.key)).toEqual(['c1', 'c2', 'c3']);
    expect(groups[2]).toMatchObject({ name: 'ChatGPT', headline: null, headlineAt: null, windows: [], newest: null });
    // without a week the fullest total window is the headline
    expect(groups[4]!.headline).toMatchObject({ window: FULLEST, usedPercent: 95 });
    expect(
      providerGroups([rowOf('q1', 'quux', [w('five_hour', 20), w(FULLEST, 70), w('weekly:opus', 40)])])[0]!.headline,
    ).toMatchObject({ window: FULLEST, usedPercent: 70, freePercent: 30 });
    // least left first: zeta (5 %), claude (35 %), yota (40 %, although one of its accounts is used up)
    expect(providerGroups(rows, true).map((group) => group.provider)).toEqual([
      'zeta',
      'claude',
      'yota',
      'alpha',
      'openai',
    ]);
    expect(providerGroups([])).toEqual([]);
  });

  it('colours by what is left', () => {
    expect([100, 30.1, 30, 10.1, 10, 0].map(freeLevel)).toEqual(['ok', 'ok', 'warn', 'warn', 'low', 'low']);
  });

  it('writes the time to a reset and the age of a read', () => {
    const now = ms(AT);
    expect(untilParts(now - 1000, now)).toEqual({ kind: 'due', a: 0, b: 0 });
    expect(untilParts(now, now)).toEqual({ kind: 'due', a: 0, b: 0 });
    expect(untilParts(now + 30_000, now)).toEqual({ kind: 'soon', a: 0, b: 0 });
    expect(untilParts(now + 5 * 60_000, now)).toEqual({ kind: 'min', a: 5, b: 0 });
    expect(untilParts(now + 125 * 60_000, now)).toEqual({ kind: 'h', a: 2, b: 5 });
    expect(untilParts(now + 27 * 3600_000, now)).toEqual({ kind: 'd', a: 1, b: 3 });
    expect(ageParts(now + 5000, now)).toEqual({ unit: 's', value: 0 });
    expect(ageParts(now - 30_000, now)).toEqual({ unit: 's', value: 30 });
    expect(ageParts(now - 5 * 60_000, now)).toEqual({ unit: 'min', value: 5 });
    expect(ageParts(now - 3 * 3600_000, now)).toEqual({ unit: 'h', value: 3 });
    expect(ageParts(now - 50 * 3600_000, now)).toEqual({ unit: 'd', value: 2 });
  });
});
