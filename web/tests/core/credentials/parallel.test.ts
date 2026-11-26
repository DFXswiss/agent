// U-WEB-credentials (KOR-73, ADR-099): subscriptions logged in side by side – the own mailbox of a subscription in its
// sealed record (only the address leaves), the record written before its login, the login with `extended`, its slot
// and the data of a new account, several logins at once, the human check (`verify`, click, relay), the reason of a
// fallback, the check of the own mailbox, and the bodies and the `slot` of the request client.
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { fromUtf8 } from '../../../src/core/encoding/bytes';
import { openRequest } from '../../../src/core/crypto/request';
import { loginProgress, sendClick, sendCode, sendRelay, startCheck } from '../../../src/core/credentials/login';
import {
  addSubscriptions,
  createSubscription,
  isMailbox,
  isSubscriptionRecord,
  loadSubscriptions,
  newRecordBody,
  setSubscriptionMailbox,
  startSubscriptionLogin,
  subscriptionDevices,
} from '../../../src/core/credentials/subscriptions';
import { checkResponseBody, isAccount, validRequestBody } from '../../../src/core/requests/bodies';
import type { OpenedResponse, RequestView } from '../../../src/core/requests/client';
import { prepareRequest, submitRequest } from '../../../src/core/requests/client';
import { A, B, body, ORG, world } from './sub-hub';

const MAILBOX = {
  imap_host: 'imap.example.com',
  imap_port: 993,
  imap_user: 'sub3@example.com',
  imap_password: 'mailbox-password-of-a-test',
  email: 'sub3@example.com',
};
const IMAGE = { width: 960, height: 600, data: '/9j/4AAQSkZJRgABAQ==' };

type World = Awaited<ReturnType<typeof world>>;

function devices(t: World['t'], features: string[]) {
  t.client.on('GET /v1/me/devices', () => ({
    devices: [
      {
        device_id: t.device.id,
        kind: 'platform',
        org_id: t.org,
        wg_ids: [t.wg],
        online: true,
        last_seen_at: null,
        features,
      },
    ],
  }));
}

function accept(t: World['t']) {
  t.client.on('POST /v1/device-requests', (sent) => ({
    request_id: (sent as { envelope: { header: { id: string } } }).envelope.header.id,
    state: 'pending',
  }));
}

async function sent(t: World['t']) {
  const found = [];
  for (const call of t.client.of('POST /v1/device-requests')) {
    const { envelope, ...plain } = call.body as { envelope: never };
    const opened = await openRequest(t.device.identity.kem, envelope, b64uDecode(t.me.identity.public().sig));
    found.push({ plain, body: JSON.parse(fromUtf8(opened)) as Record<string, unknown> });
  }
  return found;
}

const ok = (seq: number, content: unknown, final = false): OpenedResponse =>
  ({ seq, status: 'verified', final, at: 0, body: content }) as OpenedResponse;
const view = (state: string, responses: OpenedResponse[]): RequestView => ({
  id: 'r1',
  state,
  expiresAt: 0,
  responses,
});

describe('the own mailbox', () => {
  it('belongs to the record form and only its address is listed', async () => {
    expect(isMailbox(MAILBOX)).toBe(true);
    for (const bad of [
      null,
      { ...MAILBOX, extra: 1 },
      { ...MAILBOX, imap_host: 'a b' },
      { ...MAILBOX, imap_port: 0 },
      { ...MAILBOX, imap_port: 65536 },
      { ...MAILBOX, imap_port: 1.5 },
      { ...MAILBOX, imap_user: '' },
      { ...MAILBOX, imap_password: '' },
      { ...MAILBOX, imap_password: 'x'.repeat(1025) },
      { ...MAILBOX, imap_host: 1 },
      { ...MAILBOX, imap_user: 1 },
      { ...MAILBOX, imap_password: 1 },
      { ...MAILBOX, email: 1 },
      { ...MAILBOX, email: 'no' },
      { ...MAILBOX, email: `${'a'.repeat(64)}@${'b'.repeat(190)}.com` },
    ])
      expect(isMailbox(bad)).toBe(false);
    expect(isSubscriptionRecord(body({ mailbox: MAILBOX }))).toBe(true);
    expect(isSubscriptionRecord(body({ mailbox: { ...MAILBOX, imap_port: 'x' } }))).toBe(false);
    const { t, hub } = await world();
    await hub.add(A, body({ name: 'Own', mailbox: MAILBOX }));
    await hub.add(B, body({ name: 'Single' }));
    const found = await loadSubscriptions(t.context);
    expect(Object.fromEntries(found.map((item) => [item.id, item.mailbox]))).toEqual({
      [A]: MAILBOX.email,
      [B]: null,
    });
    expect(JSON.stringify(found)).not.toContain(MAILBOX.imap_password);
  });

  it('is set, replaced and removed as a new version; a malformed one is refused', async () => {
    const { t, hub } = await world();
    await hub.add(A, body({ name: 'Own' }));
    await setSubscriptionMailbox(t.context, A, MAILBOX);
    expect((await hub.opened(A)).mailbox).toEqual(MAILBOX);
    await setSubscriptionMailbox(t.context, A, { ...MAILBOX, imap_port: 143 });
    expect((await hub.opened(A)).mailbox).toMatchObject({ imap_port: 143 });
    await setSubscriptionMailbox(t.context, A, null);
    expect('mailbox' in (await hub.opened(A))).toBe(false);
    expect(() => setSubscriptionMailbox(t.context, A, { ...MAILBOX, email: 'no' })).toThrow();
  });

  it('is checked on the device with the subscription named', async () => {
    const { t } = await world();
    accept(t);
    await startCheck(t.context, t.device.id, 'mailbox', A);
    expect((await sent(t))[0]!.body).toEqual({ subscription_id: A });
    expect(validRequestBody('mailbox.check', { subscription_id: 'x' }, false)).toBe(false);
    expect(validRequestBody('mailbox.check', { other: A }, false)).toBe(false);
  });
});

describe('the record before its login', () => {
  it('holds label, note, address, mailbox and use; nothing more', () => {
    expect(newRecordBody({ name: ' Team ', note: ' ', email: ' ' })).toEqual({
      email: null,
      kind: 'oauth_login',
      name: 'Team',
    });
    expect(
      newRecordBody({ name: 'Team', note: 'n', email: 'a@example.com', mailbox: MAILBOX, orgs: [ORG, ORG] }),
    ).toEqual({
      email: 'a@example.com',
      kind: 'oauth_login',
      name: 'Team',
      note: 'n',
      mailbox: MAILBOX,
      use: { pool: false, orgs: [ORG] },
    });
    expect(() => newRecordBody({ name: '' })).toThrow();
  });

  it('is written with version 0 expected and the mirror of its use', async () => {
    const { t, hub } = await world();
    await createSubscription(t.context, A, { name: 'Team', orgs: [ORG] });
    expect(hub.puts.at(-1)!.body).toMatchObject({ expected_version: 0, use: { pool: false, orgs: [ORG] } });
    await createSubscription(t.context, B, { name: 'Other' });
    expect(hub.puts.at(-1)!.body.use).toEqual({ pool: false, orgs: [] });
    expect(await hub.opened(B)).toEqual({ email: null, kind: 'oauth_login', name: 'Other' });
    // a retry after the write went through leaves the record as it is
    const writes = hub.puts.length;
    await createSubscription(t.context, B, { name: 'Changed' });
    expect(hub.puts).toHaveLength(writes);
    expect(await hub.opened(B)).toMatchObject({ name: 'Other' });
    t.store.update('me', (me) => ({ ...me, userId: null }));
    await expect(createSubscription(t.context, A, { name: 'x' })).rejects.toMatchObject({ code: 'unauthorized' });
  });
});

describe('the parallel login', () => {
  it('names the device that runs logins side by side', async () => {
    const { t } = await world();
    devices(t, ['subscriptions']);
    expect((await subscriptionDevices(t.context)).parallel).toBe(false);
    devices(t, ['subscriptions', 'subscriptions.parallel']);
    expect(await subscriptionDevices(t.context)).toMatchObject({ preselected: t.device.id, parallel: true });
    devices(t, ['subscriptions.parallel']);
    expect(await subscriptionDevices(t.context)).toMatchObject({ preselected: null, parallel: false });
  });

  it('writes the record, then sends the automatic login with extended, slot and the new account', async () => {
    const { t, hub } = await world();
    accept(t);
    await startSubscriptionLogin(
      t.context,
      t.device.id,
      {
        subscriptionId: A,
        mode: 'manual',
        email: ' sub3@example.com ',
        create: { name: 'Team', mailbox: MAILBOX },
        account: { name: ' Ada ', birth_date: '1990-04-01', consents: ['terms', 'age', 'terms'] },
        ownMailbox: true,
      },
      true,
    );
    await startSubscriptionLogin(
      t.context,
      t.device.id,
      { subscriptionId: B, mode: 'manual', email: 'x@example.com', account: { name: 'Bo', consents: [] } },
      true,
    );
    await startSubscriptionLogin(t.context, t.device.id, { subscriptionId: B, mode: 'manual' }, true);
    expect((await hub.opened(A)).mailbox).toEqual(MAILBOX);
    const [first, second, third] = await sent(t);
    expect(first).toEqual({
      plain: { slot: A },
      body: {
        mode: 'automatic',
        subscription_id: A,
        extended: true,
        email: 'sub3@example.com',
        account: { name: 'Ada', birth_date: '1990-04-01', consents: ['terms', 'age'] },
      },
    });
    // a manual login carries neither the address nor an account
    expect(second!.body).toEqual({ mode: 'manual', subscription_id: B, extended: true });
    expect(third).toEqual({ plain: { slot: B }, body: { mode: 'manual', subscription_id: B, extended: true } });
  });

  it('refuses what needs the new path on an older device', async () => {
    const { t } = await world();
    accept(t);
    for (const input of [
      { subscriptionId: A, mode: 'manual' as const, ownMailbox: true },
      { subscriptionId: A, mode: 'manual' as const, create: { name: 'x' } },
      { subscriptionId: A, mode: 'automatic' as const, account: { name: 'x' } },
    ])
      await expect(startSubscriptionLogin(t.context, t.device.id, input)).rejects.toMatchObject({
        code: 'no_device_available',
      });
    expect(t.client.of('POST /v1/device-requests')).toHaveLength(0);
  });

  it('starts several logins at once, each with its own outcome', async () => {
    const { t, hub } = await world();
    accept(t);
    const started = await addSubscriptions(t.context, t.device.id, [
      { record: { name: 'One', mailbox: MAILBOX, orgs: [ORG] }, account: { name: 'Ada' } },
      { record: { name: 'Two' } },
      { record: { name: '' } },
    ]);
    expect(started.map((item) => ('prepared' in item ? 'started' : item.error))).toEqual([
      'started',
      'started',
      'invalid_argument',
    ]);
    const bodies = (await sent(t)).map((item) => item.body);
    expect(bodies.map((item) => item.mode).sort()).toEqual(['automatic', 'manual']);
    expect(bodies.find((item) => item.mode === 'automatic')).toMatchObject({ account: { name: 'Ada' } });
    expect(hub.rows.size).toBe(2);
  });
});

describe('the check for humans', () => {
  it('sends clicks as fractions within the picture and the relay as follow-ups', async () => {
    const { t } = await world();
    accept(t);
    const original = await startSubscriptionLogin(t.context, t.device.id, { subscriptionId: A, mode: 'manual' }, true);
    expect(original.slot).toBe(A);
    const click = await sendClick(t.context, original, 1.5, -0.2);
    expect(click.follows).toBe(original.id);
    await sendClick(t.context, original, 0.25, 0.5);
    await sendRelay(t.context, original);
    await sendCode(t.context, original, 'abc');
    const found = await sent(t);
    expect(found.slice(1).map((item) => item.body)).toEqual([
      { click: { x: 1, y: 0 } },
      { click: { x: 0.25, y: 0.5 } },
      { relay: true },
      { code: 'abc' },
    ]);
    expect(found.slice(1).every((item) => !('slot' in item.plain))).toBe(true);
  });

  it('reads the newest picture and the reason of a fallback', () => {
    const verify = (frame: number) => ok(frame, { step: 'verify', frame, image: IMAGE });
    expect(loginProgress(view('delivered', [verify(1), verify(2)]), null)).toEqual({
      step: 'verify',
      frame: 2,
      image: IMAGE,
    });
    const fallback = ok(3, {
      step: 'authorize',
      url: 'https://claude.ai/x',
      automation: 'failed',
      reason: 'human_check',
    });
    expect(loginProgress(view('delivered', [verify(1), fallback]), null)).toMatchObject({
      step: 'authorize',
      reason: 'human_check',
    });
  });
});

describe('bodies', () => {
  it('accept the new follow-ups, accounts and responses of the contracts and nothing else', () => {
    const login = (content: unknown, followUp = false) => validRequestBody('claude.login', content, followUp);
    expect(login({ click: { x: 0, y: 1 } }, true)).toBe(true);
    expect(login({ relay: true }, true)).toBe(true);
    for (const bad of [
      { click: { x: 2, y: 0 } },
      { click: { x: '0', y: 0 } },
      { click: { x: 0 } },
      { click: 'x' },
      { click: { x: 0, y: 0 }, code: 'a' },
      { relay: false },
      { relay: true, code: 'a' },
    ])
      expect(login(bad, true)).toBe(false);
    expect(login({ mode: 'automatic', extended: true, account: { name: 'Ada', consents: ['marketing'] } })).toBe(true);
    expect(login({ mode: 'manual', extended: false })).toBe(false);
    expect(login({ mode: 'manual', account: { name: 'Ada' } })).toBe(false);
    expect(isAccount({ name: 'Ada', birth_date: '1990-04-01', consents: ['terms', 'age', 'marketing'] })).toBe(true);
    for (const bad of [
      null,
      { name: '' },
      { name: 'Ada', extra: 1 },
      { name: 'Ada', birth_date: '01.04.1990' },
      { name: 'Ada', consents: 'terms' },
      { name: 'Ada', consents: ['payment'] },
      { name: 'Ada', consents: ['terms', 'terms'] },
    ])
      expect(isAccount(bad)).toBe(false);
    const response = (content: unknown) => checkResponseBody('claude.login', content);
    expect(response({ step: 'verify', frame: 1, image: IMAGE })).not.toBeNull();
    expect(response({ step: 'authorize', url: 'https://claude.ai/x', reason: 'unknown_page' })).not.toBeNull();
    expect(response({ step: 'progress', stage: 'verified' })).not.toBeNull();
    for (const bad of [
      { step: 'verify', frame: 1 },
      { step: 'verify', frame: 0, image: IMAGE },
      { step: 'verify', frame: 1, image: { ...IMAGE, width: 0 } },
      { step: 'verify', frame: 1, image: { ...IMAGE, height: 5000 } },
      { step: 'verify', frame: 1, image: { ...IMAGE, data: 'a' } },
      { step: 'verify', frame: 1, image: { ...IMAGE, data: 'no spaces' } },
      { step: 'verify', frame: 1, image: { ...IMAGE, extra: 1 } },
      { step: 'verify', frame: 1, image: 'x' },
      { step: 'authorize', url: 'https://claude.ai/x', reason: 'solved' },
    ])
      expect(response(bad)).toBeNull();
  });
});

describe('the request client', () => {
  it('posts the slot of a root and refuses it on a follow-up', async () => {
    const { t } = await world();
    accept(t);
    const root = await prepareRequest(t.context, {
      device: t.device.id,
      kind: 'claude.login',
      body: { mode: 'manual' },
      slot: A,
    });
    expect(root.slot).toBe(A);
    await submitRequest(t.context, root);
    expect(t.client.of('POST /v1/device-requests')[0]!.body).toEqual({ envelope: root.envelope, slot: A });
    await expect(
      prepareRequest(t.context, {
        device: t.device.id,
        kind: 'claude.login',
        body: { code: 'x' },
        follows: root,
        slot: A,
      }),
    ).rejects.toMatchObject({ code: 'request_invalid' });
  });
});
