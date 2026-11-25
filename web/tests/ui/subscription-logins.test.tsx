// The dialogs of Claude subscriptions (KOR-69, ADR-090) in jsdom: add and log in with the relay (link, code, done) on a
// device that announced `subscriptions`, no device, a refused start, "log in now" for a subscription that is not
// logged in, name and note, the use (only measure, organizations, the operator pool), log out with the place to revoke,
// remove with a confirmation, failures – and that no token reaches the DOM.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { bodyJson } from '../../src/core/encoding/json';
import { sealResponse } from '../../src/core/crypto/request';
import { HubError } from '../../src/core/net/signedFetch';
import { noteResponse } from '../../src/core/store/slices/requests';
import { SubscriptionManager } from '../../src/ui/subscriptions/SubscriptionManager';
import { SubscriptionsScreen } from '../../src/ui/screens/subscriptions/SubscriptionsScreen';
import { setPhase } from '../../src/core/store/slices/session';
import type { CredWorld } from '../core/credentials/world';
import { A, body, ORG, reading, SubHub, TOKEN, world } from '../core/credentials/sub-hub';
import { mount } from './chat-world';

interface Sent {
  id: string;
  body: unknown;
  bodies: { body: unknown; final: boolean }[];
  tick: number;
}

function device(t: CredWorld) {
  const sent: Sent[] = [];
  let refuse: HubError | null = null;
  t.client.on('POST /v1/device-requests', (posted) => {
    if (refuse !== null) throw refuse;
    const header = (posted as { envelope: { header: { id: string; kind: string } } }).envelope.header;
    const request: Sent = { id: header.id, body: posted, bodies: [], tick: 0 };
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
  return { sent, respond, refuse: (error: HubError | null) => (refuse = error) };
}

async function page(options: { features?: string[]; seed?: (hub: SubHub) => Promise<void> } = {}) {
  const { t, hub } = await world();
  await options.seed?.(hub);
  t.client.on('GET /v1/me/devices', () => ({
    devices: [
      {
        device_id: t.device.id,
        kind: 'platform',
        org_id: t.org,
        wg_ids: [t.wg],
        online: true,
        last_seen_at: null,
        features: options.features ?? ['subscriptions'],
      },
    ],
  }));
  const d = device(t);
  mount(t, [{ path: '/s', element: <SubscriptionManager /> }], '/s');
  return { t, hub, d };
}

const noToken = () => expect(document.body.innerHTML).not.toContain(TOKEN);

describe('SubscriptionManager', () => {
  it('adds a subscription and logs it in with the link and the code', async () => {
    const { hub, d } = await page();
    expect(await screen.findByTestId('submanage.empty')).toBeVisible();
    fireEvent.click(screen.getByTestId('submanage.add'));
    const start = screen.getByTestId('subadd.start');
    expect(start).toBeDisabled();
    fireEvent.change(screen.getByTestId('subadd.name'), { target: { value: 'Team 2' } });
    fireEvent.change(screen.getByTestId('subadd.note'), { target: { value: 'reviews' } });
    fireEvent.click(start);
    expect(await screen.findByTestId('claude.waiting')).toBeVisible();
    await d.respond(0, { step: 'authorize', url: 'https://claude.ai/oauth/authorize?x=1' });
    expect(await screen.findByTestId('claude.authorize.open')).toHaveAttribute('target', '_blank');
    fireEvent.change(screen.getByTestId('claude.code'), { target: { value: 'abc#123' } });
    fireEvent.click(screen.getByTestId('claude.code.send'));
    await screen.findByText('Code sent');
    await hub.add(A, body({ name: 'Team 2', note: 'reviews' }), { reading: reading(25) });
    await d.respond(1, { step: 'done' }, true);
    expect(await screen.findByTestId(`subcard.${A}`)).toHaveTextContent('Team 2');
    expect(screen.queryByTestId('subadd')).toBeNull();
    noToken();
  });

  it('closes the form, reports a missing device and a refused start', async () => {
    const { d } = await page({ features: [] });
    fireEvent.click(await screen.findByTestId('submanage.add'));
    fireEvent.click(screen.getByTestId('subadd.close'));
    fireEvent.click(screen.getByTestId('submanage.add'));
    fireEvent.change(screen.getByTestId('subadd.name'), { target: { value: 'X' } });
    fireEvent.click(screen.getByTestId('subadd.start'));
    expect(await screen.findByTestId('sublogin.no-device')).toBeVisible();
    fireEvent.click(screen.getByTestId('sublogin.close'));
    expect(screen.getByTestId('submanage.add')).toBeVisible();
    expect(d.sent).toHaveLength(0);
  });

  it('retries a refused start', async () => {
    const { d } = await page();
    d.refuse(new HubError('rate_limited', 429));
    fireEvent.click(await screen.findByTestId('submanage.add'));
    fireEvent.change(screen.getByTestId('subadd.name'), { target: { value: 'X' } });
    fireEvent.click(screen.getByTestId('subadd.start'));
    expect(await screen.findByTestId('sublogin.failed')).toHaveAttribute('data-code', 'rate_limited');
    d.refuse(null);
    fireEvent.click(screen.getByTestId('sublogin.retry'));
    expect(await screen.findByTestId('claude.waiting')).toBeVisible();
  });

  it('shows a load failure and tries again', async () => {
    const { t } = await world();
    let fail = true;
    t.client.on('GET /v1/me/subscriptions', () => {
      if (fail) throw new HubError('unavailable', 503);
      return { subscriptions: [] };
    });
    mount(t, [{ path: '/s', element: <SubscriptionManager /> }], '/s');
    expect(await screen.findByTestId('submanage.failed')).toHaveAttribute('data-code', 'unavailable');
    fail = false;
    fireEvent.click(screen.getByTestId('submanage.retry'));
    expect(await screen.findByTestId('submanage.empty')).toBeVisible();
  });
});

async function withCard(extra: Parameters<SubHub['add']>[2] = {}, content: unknown = body({ name: 'Team' })) {
  return page({ seed: (hub) => hub.add(A, content, { reading: reading(10), ...extra }) });
}

describe('SubscriptionCard', () => {
  it('logs a subscription in that is not logged in, and in again', async () => {
    const { hub, d } = await page({
      seed: (seeded) => seeded.add(A, { email: null, kind: 'setup_token', name: 'Out' }),
    });
    const card = await screen.findByTestId(`subcard.${A}`);
    expect(within(card).getByTestId('subcard.status')).toHaveAttribute('data-status', 'not_logged_in');
    expect(within(card).queryByTestId('subcard.logout')).toBeNull();
    fireEvent.click(within(card).getByTestId('subcard.login'));
    await screen.findByTestId('claude.waiting');
    fireEvent.click(screen.getByTestId('sublogin.close'));
    fireEvent.click(within(card).getByTestId('subcard.login'));
    await screen.findByTestId('claude.waiting');
    await hub.add(A, body({ name: 'Out' }));
    await d.respond(1, { step: 'done' }, true);
    expect(await screen.findByText('Logged in')).toBeVisible();
  });

  it('changes name and note, the use and logs out', async () => {
    const { t, hub } = await withCard();
    t.store.update('orgs', (orgs) => ({ ...orgs, platform: { state: { hasRole: () => true } } as never }));
    const card = await screen.findByTestId(`subcard.${A}`);
    fireEvent.click(within(card).getByTestId('subcard.edit'));
    fireEvent.click(within(card).getByTestId('subcard.cancel'));
    fireEvent.click(within(card).getByTestId('subcard.edit'));
    fireEvent.change(within(card).getByTestId('subcard.name'), { target: { value: 'New' } });
    fireEvent.change(within(card).getByTestId('subcard.note'), { target: { value: 'for reviews' } });
    fireEvent.click(within(card).getByTestId('subcard.save'));
    expect(await screen.findByText('New')).toBeVisible();
    expect(await hub.opened(A)).toMatchObject({ name: 'New', note: 'for reviews' });
    const again = await screen.findByTestId(`subcard.${A}`);
    fireEvent.click(within(again).getByTestId('subcard.edit'));
    const writes = hub.puts.length;
    fireEvent.click(within(again).getByTestId('subcard.save')); // nothing changed: nothing written
    await waitFor(() => expect(screen.queryByTestId('subcard.labels')).toBeNull());
    expect(hub.puts).toHaveLength(writes);
    const fresh = await screen.findByTestId(`subcard.${A}`);
    fireEvent.click(await within(fresh).findByTestId('subcard.use'));
    const again2 = fresh;
    fireEvent.click(within(again2).getByTestId('subuse.shared'));
    fireEvent.click(within(again2).getByTestId(`subuse.org.${t.org}`));
    fireEvent.click(within(again2).getByTestId(`subuse.org.${t.org}`));
    fireEvent.click(within(again2).getByTestId(`subuse.org.${t.org}`));
    fireEvent.click(within(again2).getByTestId('subuse.pool'));
    fireEvent.click(within(again2).getByTestId('subuse.save'));
    expect(await screen.findByTestId('subuse.saved')).toBeVisible();
    expect((await hub.opened(A)).use).toEqual({ pool: true, orgs: [t.org] });
    const used = await screen.findByTestId(`subcard.${A}`); // the editor stays open after a save
    fireEvent.click(within(used).getByTestId('subuse.measure'));
    hub.failPut = [new HubError('forbidden', 403)];
    fireEvent.click(within(used).getByTestId('subuse.save'));
    expect(await screen.findByTestId('subuse.failed')).toHaveAttribute('data-code', 'forbidden');
    fireEvent.click(within(used).getByTestId('subuse.save'));
    await screen.findByTestId('subuse.saved');
    expect('use' in (await hub.opened(A))).toBe(false);
    const measured = await screen.findByTestId(`subcard.${A}`);
    fireEvent.click(within(measured).getByTestId('subcard.use.close'));
    fireEvent.click(within(measured).getByTestId('subcard.logout'));
    expect(await screen.findByTestId('subcard.revoke')).toHaveTextContent('claude.ai/settings');
    expect('token' in (await hub.opened(A))).toBe(false);
    noToken();
  });

  it('removes after a confirmation and reports a failure', async () => {
    const { t, hub } = await withCard({}, body({ name: 'Team', use: { pool: false, orgs: [ORG] } }));
    t.store.update('orgs', (orgs) => ({ ...orgs, platform: null }));
    const card = await screen.findByTestId(`subcard.${A}`);
    fireEvent.click(within(card).getByTestId('subcard.use'));
    expect(within(card).queryByTestId('subuse.pool')).toBeNull(); // no verified platform log: no operator pool
    fireEvent.click(within(card).getByTestId('subcard.use.close'));
    fireEvent.click(within(card).getByTestId('subcard.remove'));
    fireEvent.click(within(card).getByTestId('subcard.remove.no'));
    fireEvent.click(within(card).getByTestId('subcard.remove'));
    hub.failDelete = new HubError('forbidden', 403);
    fireEvent.click(within(card).getByTestId('subcard.remove.yes'));
    expect(await screen.findByTestId('subcard.failed')).toHaveAttribute('data-code', 'forbidden');
    hub.failDelete = null;
    fireEvent.click(within(card).getByTestId('subcard.remove.yes'));
    expect(await screen.findByTestId('submanage.empty')).toBeVisible();
  });

  it('shows the primary and an unverifiable subscription without actions', async () => {
    const other = '0190f000-0000-7000-8000-0000000000c7';
    await page({
      seed: async (hub) => {
        await hub.add(A, body(), { primary: true });
        await hub.add(other, body(), { owner: ORG });
      },
    });
    const primary = await screen.findByTestId(`subcard.${A}`);
    expect(primary).toHaveTextContent('Own sessions');
    expect(within(primary).queryByTestId('subcard.login')).toBeNull();
    const broken = screen.getByTestId(`subcard.${other}`);
    expect(broken).toHaveTextContent('Subscription');
    expect(within(broken).getByTestId('subcard.status')).toHaveAttribute('data-status', 'unverifiable');
  });
});

describe('settings section', () => {
  it('shows the manager under its title', async () => {
    const { t } = await world();
    const { section, SubscriptionLoginsSection } = await import('../../src/ui/settings/sections/subscription-logins');
    expect(section).toMatchObject({ id: 'subscription-logins', titleKey: 'subscription-login.title' });
    mount(t, [{ path: '/s', element: <SubscriptionLoginsSection /> }], '/s');
    expect(await screen.findByTestId('submanage.empty')).toBeVisible();
    expect(screen.getByTestId('subscription-logins.section')).toHaveTextContent('platform device');
  });
});

describe('the page My subscriptions', () => {
  it('adds a subscription and logs one in from the page', async () => {
    const { t, hub } = await world();
    await hub.add(A, { email: null, kind: 'setup_token', name: 'Out' });
    t.client.on('GET /v1/me/devices', () => ({
      devices: [
        {
          device_id: t.device.id,
          kind: 'platform',
          org_id: t.org,
          wg_ids: [t.wg],
          online: true,
          last_seen_at: null,
          features: ['subscriptions'],
        },
      ],
    }));
    const d = device(t);
    setPhase(t.store, 'unlocked');
    mount(t, [{ path: '/subscriptions', element: <SubscriptionsScreen /> }], '/subscriptions');
    fireEvent.click(await screen.findByTestId('subscriptions.row.login'));
    await waitFor(() => expect(d.sent).toHaveLength(1));
    await hub.add(A, body({ name: 'Out' }));
    await d.respond(0, { step: 'done' }, true);
    await waitFor(() => expect(screen.queryByTestId('subscriptions.row.login')).toBeNull());
    fireEvent.click(screen.getByTestId('subscriptions.add'));
    fireEvent.change(screen.getByTestId('subadd.name'), { target: { value: 'New' } });
    fireEvent.click(screen.getByTestId('subadd.start'));
    await waitFor(() => expect(d.sent).toHaveLength(2));
    await d.respond(1, { step: 'done' }, true);
    expect(await screen.findByTestId('subscriptions.add')).toBeVisible();
  });
});
