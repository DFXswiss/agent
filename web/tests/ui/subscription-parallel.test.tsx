// The dialogs of subscriptions logged in side by side (KOR-73, ADR-098) in jsdom, with a device that announced
// `subscriptions.parallel`: adding one with its own mailbox, a new account and organizations (the record is written
// first, the login carries `extended`, the account and its slot), the check for humans (picture, click, relay, the
// reason of the fallback), adding several at once with progress per subscription, and the own mailbox on the card
// (set, check, remove) – and that no mailbox password reaches the DOM.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../src/core/encoding/b64u';
import { fromUtf8 } from '../../src/core/encoding/bytes';
import { bodyJson } from '../../src/core/encoding/json';
import { openRequest, sealResponse } from '../../src/core/crypto/request';
import { HubError } from '../../src/core/net/signedFetch';
import { noteResponse } from '../../src/core/store/slices/requests';
import { SubscriptionManager } from '../../src/ui/subscriptions/SubscriptionManager';
import type { CredWorld } from '../core/credentials/world';
import { A, body, reading, type SubHub, world } from '../core/credentials/sub-hub';
import { mount } from './chat-world';

const PARALLEL = ['subscriptions', 'subscriptions.parallel'];
const PASSWORD = 'mailbox-password-of-a-test';
const IMAGE = { width: 960, height: 600, data: '/9j/4AAQSkZJRgABAQ==' };
const MAILBOX = {
  imap_host: 'imap.example.com',
  imap_port: 993,
  imap_user: 'sub@example.com',
  imap_password: PASSWORD,
  email: 'sub@example.com',
};

interface Sent {
  id: string;
  posted: { envelope: never; slot?: string };
  bodies: { body: unknown; final: boolean }[];
  tick: number;
}

function device(t: CredWorld) {
  const sent: Sent[] = [];
  let refuse: HubError | null = null;
  t.client.on('POST /v1/device-requests', (posted) => {
    if (refuse !== null) throw refuse;
    const header = (posted as { envelope: { header: { id: string; kind: string } } }).envelope.header;
    const request: Sent = { id: header.id, posted: posted as Sent['posted'], bodies: [], tick: 0 };
    sent.push(request);
    t.client.on(`GET /v1/device-requests/${header.id}`, async () => {
      const responses = [];
      for (const [index, item] of request.bodies.entries()) {
        const response = {
          v: 1,
          id: `0190f000-0000-7000-8000-0000000${String(sent.indexOf(request)).padStart(2, '0')}${index}f0`,
          user: t.me.id,
          device: t.device.id,
          kind: header.kind,
          in_reply_to: request.id,
          seq: index + 1,
          final: item.final,
          ts: '2026-11-21T12:00:01.000Z',
          enc_alg: 'HPKE-X25519-A256GCM',
        };
        const envelope = await sealResponse(
          response,
          t.me.identity.kemPublic(),
          bodyJson(item.body),
          t.device.identity.sig.privateKey,
        );
        responses.push({ seq: index + 1, envelope, created_at: response.ts });
      }
      return {
        request_id: request.id,
        kind: header.kind,
        device_id: t.device.id,
        state: 'delivered',
        created_at: '2026-11-21T12:00:00.000Z',
        expires_at: '2026-11-21T12:15:00.000Z',
        responses,
      };
    });
    return { request_id: header.id, state: 'pending' };
  });
  const respond = async (index: number, answer: unknown, final = false) => {
    const request = sent[index]!;
    request.bodies.push({ body: answer, final });
    request.tick += 1;
    await act(async () => noteResponse(t.store, request.id, request.tick));
  };
  const opened = async (index: number) => {
    const envelope = sent[index]!.posted.envelope;
    const plain = await openRequest(t.device.identity.kem, envelope, b64uDecode(t.me.identity.public().sig));
    return JSON.parse(fromUtf8(plain)) as Record<string, unknown>;
  };
  return { sent, respond, opened, refuse: (error: HubError | null) => (refuse = error) };
}

function features(t: CredWorld, announced: string[]) {
  t.client.on('GET /v1/me/devices', () => ({
    devices: [
      {
        device_id: t.device.id,
        kind: 'platform',
        org_id: t.org,
        wg_ids: [t.wg],
        online: true,
        last_seen_at: null,
        features: announced,
      },
    ],
  }));
}

async function page(options: { seed?: (hub: SubHub) => Promise<void> } = {}) {
  const { t, hub } = await world();
  await options.seed?.(hub);
  features(t, PARALLEL);
  const d = device(t);
  mount(t, [{ path: '/s', element: <SubscriptionManager /> }], '/s');
  return { t, hub, d };
}

const type = (testId: string, value: string) => fireEvent.change(screen.getByTestId(testId), { target: { value } });

function fillMailbox(prefix: string) {
  type(`${prefix}.host`, MAILBOX.imap_host);
  type(`${prefix}.user`, MAILBOX.imap_user);
  type(`${prefix}.password`, PASSWORD);
  type(`${prefix}.email`, MAILBOX.email);
}

const noPassword = () => expect(document.body.innerHTML).not.toContain(PASSWORD);

describe('adding one subscription on a parallel device', () => {
  it('writes the record with its mailbox and use, logs in automatically and lets the owner do the human check', async () => {
    const { t, hub, d } = await page();
    fireEvent.click(await screen.findByTestId('submanage.add'));
    const fields = await screen.findByTestId('subadd.mailbox');
    expect(screen.queryByTestId('subadd.automatic')).toBeNull();
    type('subadd.name', 'Team 3');
    fillMailbox('subadd.mailbox');
    type('subadd.mailbox.port', 'x');
    expect(screen.getByTestId('subadd.start')).toBeDisabled();
    type('subadd.mailbox.port', '993');
    expect(fields).toBeVisible();
    fireEvent.click(screen.getByTestId('subadd.account.on'));
    type('subadd.account.name', 'Ada Example');
    type('subadd.account.birth', '1990-04-01');
    const terms = screen.getByTestId('subadd.account.consent.terms') as HTMLInputElement;
    expect(terms.checked).toBe(false);
    fireEvent.click(terms);
    fireEvent.click(screen.getByTestId('subadd.account.consent.marketing'));
    fireEvent.click(screen.getByTestId('subadd.account.consent.marketing'));
    fireEvent.click(screen.getByTestId(`subadd.orgs.${t.org}`));
    fireEvent.click(screen.getByTestId(`subadd.orgs.${t.org}`));
    fireEvent.click(screen.getByTestId(`subadd.orgs.${t.org}`));
    type('subadd.email', 'alias@example.com');
    fireEvent.click(screen.getByTestId('subadd.start'));
    await screen.findByTestId('claude.waiting');
    const id = hub.puts[0]!.id;
    expect(await hub.opened(id)).toEqual({
      email: 'alias@example.com',
      kind: 'oauth_login',
      name: 'Team 3',
      mailbox: MAILBOX,
      use: { pool: false, orgs: [t.org] },
    });
    expect(d.sent[0]!.posted.slot).toBe(id);
    expect(await d.opened(0)).toEqual({
      mode: 'automatic',
      subscription_id: id,
      extended: true,
      email: 'alias@example.com',
      account: { name: 'Ada Example', birth_date: '1990-04-01', consents: ['terms'] },
    });
    await d.respond(0, { step: 'progress', stage: 'registered' });
    expect(await screen.findByTestId('claude.progress')).toHaveAttribute('data-stage', 'registered');
    await d.respond(0, { step: 'verify', frame: 1, image: IMAGE });
    const picture = await screen.findByTestId('claude.verify.picture');
    picture.getBoundingClientRect = () => ({ left: 10, top: 20, width: 200, height: 100 }) as DOMRect;
    fireEvent.click(picture, { clientX: 60, clientY: 70 });
    expect(await screen.findByTestId('claude.verify.clicked')).toBeVisible();
    expect(await d.opened(1)).toEqual({ click: { x: 0.25, y: 0.5 } });
    expect(d.sent[1]!.posted.slot).toBeUndefined();
    await d.respond(0, { step: 'verify', frame: 2, image: IMAGE });
    await waitFor(() => expect(screen.getByTestId('claude.verify')).toHaveAttribute('data-frame', '2'));
    expect(screen.queryByTestId('claude.verify.clicked')).toBeNull();
    // a picture without a size yet: the click goes to the middle
    const next = screen.getByTestId('claude.verify.picture');
    next.getBoundingClientRect = () => ({ left: 0, top: 0, width: 0, height: 0 }) as DOMRect;
    fireEvent.click(next);
    await waitFor(() => expect(d.sent).toHaveLength(3));
    expect(await d.opened(2)).toEqual({ click: { x: 0.5, y: 0.5 } });
    fireEvent.click(screen.getByTestId('claude.verify.relay'));
    await waitFor(() => expect(d.sent).toHaveLength(4));
    expect(await d.opened(3)).toEqual({ relay: true });
    await d.respond(0, {
      step: 'authorize',
      url: 'https://claude.ai/oauth/authorize?x=1',
      automation: 'failed',
      reason: 'human_check',
    });
    expect(await screen.findByTestId('claude.automation-reason')).toHaveAttribute('data-reason', 'human_check');
    noPassword();
  });

  it('reports a click or a relay that could not be sent', async () => {
    const { d } = await page();
    fireEvent.click(await screen.findByTestId('submanage.add'));
    await screen.findByTestId('subadd.mailbox');
    type('subadd.name', 'Team');
    fireEvent.click(screen.getByTestId('subadd.start'));
    await screen.findByTestId('claude.waiting');
    expect(await d.opened(0)).toMatchObject({ mode: 'manual', extended: true });
    await d.respond(0, { step: 'verify', frame: 1, image: IMAGE });
    d.refuse(new HubError('rate_limited', 429));
    fireEvent.click(await screen.findByTestId('claude.verify.picture'));
    expect(await screen.findByTestId('claude.code.failed')).toHaveAttribute('data-code', 'rate_limited');
    fireEvent.click(screen.getByTestId('claude.verify.relay'));
    await waitFor(() => expect(screen.getByTestId('claude.code.failed')).toHaveAttribute('data-code', 'rate_limited'));
    expect(screen.queryByTestId('claude.verify.clicked')).toBeNull();
  });
});

describe('adding several subscriptions', () => {
  it('starts every login at once and shows the progress of each', async () => {
    const { t, hub, d } = await page();
    fireEvent.click(await screen.findByTestId('submanage.add-several'));
    const start = screen.getByTestId('subbatch.start');
    expect(start).toBeDisabled();
    type('subbatch.entry.0.name', 'One');
    type('subbatch.entry.0.email', 'one@example.com');
    fillMailbox('subbatch.entry.0.mailbox');
    fireEvent.click(screen.getByTestId('subbatch.entry.0.account.on'));
    type('subbatch.entry.0.account.name', 'Ada');
    type('subbatch.entry.1.name', 'Two');
    fireEvent.click(screen.getByTestId('subbatch.add-row'));
    type('subbatch.entry.2.email', 'no');
    expect(start).toBeDisabled();
    fireEvent.click(screen.getByTestId('subbatch.entry.2.remove'));
    fireEvent.click(screen.getByTestId(`subbatch.orgs.${t.org}`));
    expect(start).toBeEnabled();
    fireEvent.click(start);
    const progress = await screen.findByTestId('subbatch.progress');
    await waitFor(() => expect(d.sent).toHaveLength(2));
    const bodies = await Promise.all([d.opened(0), d.opened(1)]);
    expect(bodies.map((item) => item.mode).sort()).toEqual(['automatic', 'manual']);
    expect(new Set(d.sent.map((item) => item.posted.slot)).size).toBe(2);
    expect(hub.puts.map((item) => item.body.use)).toEqual([
      { pool: false, orgs: [t.org] },
      { pool: false, orgs: [t.org] },
    ]);
    const auto = bodies.findIndex((item) => item.mode === 'automatic');
    await d.respond(auto, { step: 'progress', stage: 'email_entered' });
    await d.respond(1 - auto, { step: 'authorize', url: 'https://claude.ai/oauth/authorize?x=1' });
    expect(await within(progress).findByTestId('claude.progress')).toHaveAttribute('data-stage', 'email_entered');
    expect(within(progress).getByTestId('claude.authorize')).toBeVisible();
    await d.respond(auto, { step: 'done', email: 'one@example.com' }, true);
    expect(await within(progress).findByTestId('subbatch.done')).toBeVisible();
    // the other one failed: "again" logs it in once more (its record exists)
    await d.respond(1 - auto, { step: 'failed', code: 'timeout' }, true);
    fireEvent.click(await within(progress).findByTestId('claude.retry'));
    await waitFor(() => expect(d.sent).toHaveLength(3));
    expect(await d.opened(2)).toMatchObject({ mode: 'manual', extended: true });
    expect(hub.puts).toHaveLength(2);
    noPassword();
    fireEvent.click(screen.getByTestId('subbatch.close'));
    expect(screen.getByTestId('submanage.add-several')).toBeVisible();
  });

  it('shows a refused start per subscription and starts it again', async () => {
    const { d } = await page();
    fireEvent.click(await screen.findByTestId('submanage.add-several'));
    type('subbatch.entry.0.name', 'One');
    fillMailbox('subbatch.entry.0.mailbox');
    fireEvent.click(screen.getByTestId('subbatch.entry.0.account.on'));
    type('subbatch.entry.0.account.name', 'Ada');
    fireEvent.click(screen.getByTestId('subbatch.entry.1.remove'));
    d.refuse(new HubError('rate_limited', 429));
    fireEvent.click(screen.getByTestId('subbatch.start'));
    expect(await screen.findByTestId('subbatch.start-failed')).toHaveAttribute('data-code', 'rate_limited');
    fireEvent.click(screen.getByTestId('subbatch.again'));
    expect(await screen.findByTestId('subbatch.start-failed')).toBeVisible();
    d.refuse(null);
    fireEvent.click(screen.getByTestId('subbatch.again'));
    await screen.findByTestId('claude.waiting');
    expect(await d.opened(0)).toMatchObject({ mode: 'automatic', account: { name: 'Ada' } });
  });

  it('reports no parallel device and a failed device lookup', async () => {
    const { t } = await page();
    fireEvent.click(await screen.findByTestId('submanage.add-several'));
    type('subbatch.entry.0.name', 'One');
    type('subbatch.entry.1.name', 'Two');
    features(t, ['subscriptions']);
    fireEvent.click(screen.getByTestId('subbatch.start'));
    expect(await screen.findByTestId('subbatch.no-device')).toBeVisible();
    fireEvent.click(screen.getByTestId('subbatch.close'));
    features(t, PARALLEL);
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 20)));
    fireEvent.click(screen.getByTestId('submanage.add-several'));
    type('subbatch.entry.0.name', 'One');
    type('subbatch.entry.1.name', 'Two');
    t.client.on('GET /v1/me/devices', () => {
      throw new HubError('unavailable', 503);
    });
    fireEvent.click(screen.getByTestId('subbatch.start'));
    expect(await screen.findByTestId('subbatch.failed')).toHaveAttribute('data-code', 'unavailable');
  });

  it('offers no batch without a parallel device', async () => {
    const { t } = await world();
    features(t, ['subscriptions']);
    mount(t, [{ path: '/s', element: <SubscriptionManager /> }], '/s');
    await screen.findByTestId('submanage.add');
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 20)));
    expect(screen.queryByTestId('submanage.add-several')).toBeNull();
  });
});

describe('the own mailbox on the card', () => {
  const seed = (hub: SubHub) => hub.add(A, body({ name: 'Team', mailbox: MAILBOX }), { reading: reading(10) });

  it('shows its address, logs in from it, checks it, replaces and removes it', async () => {
    const { hub, d } = await page({ seed });
    const card = await screen.findByTestId(`subcard.${A}`);
    expect(within(card).getByTestId('subcard.mailbox')).toHaveTextContent(MAILBOX.email);
    fireEvent.click(within(card).getByTestId('subcard.login'));
    await screen.findByTestId('claude.waiting');
    expect(await d.opened(0)).toEqual({ mode: 'automatic', subscription_id: A, extended: true });
    fireEvent.click(screen.getByTestId('sublogin.close'));
    fireEvent.click(within(card).getByTestId('subcard.mailbox.open'));
    expect(screen.getByTestId('submailbox.current')).toHaveTextContent(MAILBOX.email);
    fireEvent.click(screen.getByTestId('submailbox.check'));
    expect(await screen.findByTestId('submailbox.checking')).toBeVisible();
    expect(await d.opened(1)).toEqual({ subscription_id: A });
    await d.respond(1, { step: 'done', valid: true }, true);
    expect(await screen.findByTestId('submailbox.checked')).toHaveAttribute('data-valid', 'true');
    fireEvent.click(screen.getByTestId('submailbox.check'));
    await waitFor(() => expect(d.sent).toHaveLength(3));
    await d.respond(2, { step: 'done', valid: false }, true);
    expect(await screen.findByTestId('submailbox.checked')).toHaveAttribute('data-valid', 'false');
    fireEvent.click(screen.getByTestId('submailbox.check'));
    await waitFor(() => expect(d.sent).toHaveLength(4));
    await d.respond(3, { step: 'failed', code: 'upstream_unreachable' }, true);
    expect(await screen.findByTestId('submailbox.check-failed')).toHaveAttribute('data-code', 'upstream_unreachable');
    expect(screen.getByTestId('submailbox.save')).toBeDisabled();
    fillMailbox('submailbox.fields');
    type('submailbox.fields.port', '143');
    fireEvent.click(screen.getByTestId('submailbox.save'));
    await waitFor(() => expect(screen.queryByTestId('submailbox')).toBeNull());
    expect((await hub.opened(A)).mailbox).toEqual({ ...MAILBOX, imap_port: 143 });
    const again = await screen.findByTestId(`subcard.${A}`);
    fireEvent.click(within(again).getByTestId('subcard.mailbox.open'));
    fireEvent.click(screen.getByTestId('submailbox.remove'));
    await waitFor(() => expect(screen.queryByTestId('submailbox')).toBeNull());
    expect('mailbox' in (await hub.opened(A))).toBe(false);
    noPassword();
  });

  it('reports a failed write, no parallel device and a failed lookup, and closes', async () => {
    const { t, hub } = await page({ seed });
    const card = await screen.findByTestId(`subcard.${A}`);
    fireEvent.click(within(card).getByTestId('subcard.mailbox.open'));
    hub.failPut = [new HubError('forbidden', 403)];
    fireEvent.click(screen.getByTestId('submailbox.remove'));
    expect(await screen.findByTestId('submailbox.failed')).toHaveAttribute('data-code', 'forbidden');
    features(t, ['subscriptions']);
    fireEvent.click(screen.getByTestId('submailbox.check'));
    await waitFor(() =>
      expect(screen.getByTestId('submailbox.failed')).toHaveAttribute('data-code', 'no_device_available'),
    );
    t.client.on('GET /v1/me/devices', () => {
      throw new HubError('unavailable', 503);
    });
    fireEvent.click(screen.getByTestId('submailbox.check'));
    await waitFor(() => expect(screen.getByTestId('submailbox.failed')).toHaveAttribute('data-code', 'unavailable'));
    fireEvent.click(screen.getByTestId('submailbox.close'));
    expect(screen.queryByTestId('submailbox')).toBeNull();
  });

  it('sets a mailbox on a subscription without one', async () => {
    const { hub } = await page({ seed: (seeded) => seeded.add(A, body({ name: 'Team' })) });
    const card = await screen.findByTestId(`subcard.${A}`);
    expect(within(card).queryByTestId('subcard.mailbox')).toBeNull();
    fireEvent.click(within(card).getByTestId('subcard.mailbox.open'));
    expect(screen.queryByTestId('submailbox.check')).toBeNull();
    fillMailbox('submailbox.fields');
    fireEvent.click(screen.getByTestId('submailbox.save'));
    await waitFor(async () => expect((await hub.opened(A)).mailbox).toEqual(MAILBOX));
  });
});
